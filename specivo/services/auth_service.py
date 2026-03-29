"""Authentication service: login, refresh, logout, session management.

Implements:
- JWT access token generation (PyJWT, HS256, 15 min)
- Opaque refresh token generation (secrets.token_urlsafe, stored as SHA-256 hash)
- Token rotation on refresh (replay attack detection)
- Progressive account lockout on failed logins
- Session listing and targeted revocation
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

import jwt
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.constants import JWT_ALGORITHM, REFRESH_TOKEN_ENTROPY_BYTES
from specivo.core.exceptions import AppError
from specivo.core.i18n import gettext as _
from specivo.core.utils import utcnow
from specivo.models.auth import RefreshToken
from specivo.models.user import User
from specivo.services.auth_utils import hash_password, verify_password

# ---------------------------------------------------------------------------
# Constant-time enumeration guard
# ---------------------------------------------------------------------------
# Generated lazily on first use — a random bcrypt hash used when a login
# attempt targets a non-existent user. This ensures the response time is
# identical to a real wrong-password attempt (bcrypt runs either way).
# Lazy to avoid module-level get_settings() which breaks CI imports.
_ENUMERATION_GUARD_HASH: str | None = None


def _get_enumeration_guard_hash() -> str:
    global _ENUMERATION_GUARD_HASH
    if _ENUMERATION_GUARD_HASH is None:
        _ENUMERATION_GUARD_HASH = hash_password(secrets.token_urlsafe(16))
    return _ENUMERATION_GUARD_HASH


# ---------------------------------------------------------------------------
# Lockout thresholds (failures → lock duration in minutes)
# ---------------------------------------------------------------------------
_LOCKOUT_TIERS: list[tuple[int, int]] = [
    (20, 24 * 60),  # 20+ failures → 24 hours
    (10, 60),  # 10+ failures → 1 hour
    (5, 15),  # 5+ failures → 15 minutes
]


def _hash_token(raw: str) -> str:
    """SHA-256 hash of a token for storage.

    No salt needed: tokens are generated with secrets.token_urlsafe(32)
    (256 bits of entropy), making precomputation impractical.
    """
    return hashlib.sha256(raw.encode()).hexdigest()


def _make_access_token(user: User, settings) -> str:
    """Encode a JWT access token for *user*."""
    now = utcnow()
    exp = now + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": str(user.id),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)


def _make_refresh_token() -> str:
    """Return a cryptographically random, URL-safe refresh token (opaque)."""
    return secrets.token_urlsafe(REFRESH_TOKEN_ENTROPY_BYTES)


def _apply_lockout(user: User, new_count: int) -> None:
    """Set locked_until on *user* if *new_count* crosses a lockout threshold."""
    lock_minutes: int | None = None
    for threshold, minutes in _LOCKOUT_TIERS:
        if new_count >= threshold:
            lock_minutes = minutes
            break
    if lock_minutes is not None:
        user.locked_until = utcnow() + timedelta(minutes=lock_minutes)


class AuthService:
    """Stateless service class — all state lives in the DB session."""

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def login(
        self,
        session: AsyncSession,
        login_or_email: str,
        password: str,
        device_info: str | None = None,
        ip: str | None = None,
    ) -> tuple[str, str]:
        """Authenticate a user and return *(access_token, refresh_token_raw)*.

        Raises ``UnauthorizedError`` with a specific code on any failure.
        Uses a generic message for wrong credentials to prevent user enumeration.
        """
        settings = get_settings()
        normalized = login_or_email.strip().lower()

        # 1. Look up by login or email (case-insensitive)
        user = await self._find_user(session, normalized)

        # --- Constant-time guard: prevent user enumeration ---
        # We always try to verify a password even when the user is not found,
        # then raise the same generic error.
        if user is None:
            # Burn time comparable to bcrypt.checkpw so timing doesn't reveal existence
            verify_password(password, _get_enumeration_guard_hash())
            raise AppError(
                code="auth_invalid_credentials",
                message=_("Invalid login or password"),
                status_code=401,
            )

        # 2. Check account status
        if user.status == "pending_verification":
            raise AppError(
                code="auth_email_not_verified",
                message=_("Email address not verified"),
                status_code=401,
            )
        if user.status == "deactivated":
            raise AppError(
                code="auth_account_deactivated",
                message=_("Account has been deactivated"),
                status_code=401,
            )

        # 3. Check active lockout
        if user.locked_until is not None and user.locked_until > utcnow():
            raise AppError(
                code="auth_account_locked",
                message=_("Account is temporarily locked due to too many failed login attempts"),
                status_code=401,
                details={"locked_until": user.locked_until.isoformat()},
            )

        # 4. Verify password
        if not user.password_hash or not verify_password(password, user.password_hash):
            # 5. Increment failure counter and apply lockout if threshold reached.
            # MUST commit before raising — otherwise get_db rollback loses the counter.
            new_count = (user.failed_login_count or 0) + 1
            user.failed_login_count = new_count
            _apply_lockout(user, new_count)
            await session.commit()
            raise AppError(
                code="auth_invalid_credentials",
                message=_("Invalid login or password"),
                status_code=401,
            )

        # 6. Success: reset failure counter and update last login.
        # Note: admin-locked accounts (status="locked", locked_until=NULL) are
        # caught by the status check at step 2 only if we add "locked" there.
        # Per the wiki spec, status="locked" blocks login regardless of locked_until.
        # Brute-force auto-locks set locked_until; those are already caught at step 3.
        # A status="locked" with a past or NULL locked_until means an admin action — block.
        if user.status == "locked":
            # Brute-force lock with future locked_until is caught above at step 3.
            # If we reach here with status=locked, the admin has locked the account
            # (locked_until=NULL) or a time-based lock exists that hasn't expired —
            # but both were caught at step 3. This branch handles the edge case
            # where status="locked" but locked_until is NULL (admin permanent lock).
            if user.locked_until is None:
                raise AppError(
                    code="auth_account_locked",
                    message=_("Account is locked. Contact an administrator."),
                    status_code=401,
                )
            # locked_until is in the past — clear the lock and allow login
            user.status = "active"

        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = utcnow()

        # 7 & 8. Generate tokens
        access_token = _make_access_token(user, settings)
        refresh_raw = _make_refresh_token()
        await self._store_refresh_token(session, user.id, refresh_raw, device_info, ip, settings)

        await session.flush()
        return access_token, refresh_raw

    async def refresh(
        self,
        session: AsyncSession,
        refresh_token_raw: str,
    ) -> tuple[str, str]:
        """Rotate a refresh token and return *(access_token, new_refresh_token_raw)*.

        If the token is not found (already used or never existed) we raise 401.
        Replay detection: if a token is reused after rotation, the caller should
        revoke all sessions for the user (handled by the router layer via logout_all).
        """
        settings = get_settings()
        token_hash = _hash_token(refresh_token_raw)

        # 1. Find the token record
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()

        if record is None:
            raise AppError(
                code="auth_refresh_expired",
                message=_("Refresh token expired or revoked"),
                status_code=401,
            )

        # 2. Check expiry
        if record.expires_at < utcnow():
            await session.delete(record)
            await session.flush()
            raise AppError(
                code="auth_refresh_expired",
                message=_("Refresh token expired or revoked"),
                status_code=401,
            )

        # Load the associated user
        user_result = await session.execute(select(User).where(User.id == record.user_id))
        user = user_result.scalar_one_or_none()
        if user is None or user.status not in ("active", "locked"):
            # User deleted or deactivated — refuse refresh
            await session.delete(record)
            await session.flush()
            raise AppError(
                code="auth_refresh_expired",
                message=_("Refresh token expired or revoked"),
                status_code=401,
            )

        # 3. Delete old token (rotation)
        await session.delete(record)

        # 4 & 5. Issue new tokens
        new_refresh_raw = _make_refresh_token()
        await self._store_refresh_token(
            session,
            user.id,
            new_refresh_raw,
            record.device_info,
            record.ip_address,
            settings,
        )
        new_access_token = _make_access_token(user, settings)

        await session.flush()
        return new_access_token, new_refresh_raw

    async def logout(
        self,
        session: AsyncSession,
        refresh_token_raw: str,
    ) -> None:
        """Revoke a single refresh token. Silent if not found."""
        token_hash = _hash_token(refresh_token_raw)
        stmt = delete(RefreshToken).where(RefreshToken.token_hash == token_hash)
        await session.execute(stmt)
        await session.flush()

    async def logout_all(
        self,
        session: AsyncSession,
        user_id: int,
    ) -> int:
        """Revoke all refresh tokens for *user_id*. Returns the count deleted."""
        stmt = delete(RefreshToken).where(RefreshToken.user_id == user_id)
        result = await session.execute(stmt)
        await session.flush()
        return result.rowcount

    async def list_sessions(
        self,
        session: AsyncSession,
        user_id: int,
    ) -> list[RefreshToken]:
        """Return all non-expired refresh tokens for *user_id*."""
        stmt = (
            select(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.expires_at > func.now(),
            )
            .order_by(RefreshToken.created_at.desc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def revoke_session(
        self,
        session: AsyncSession,
        user_id: int,
        token_id: int,
    ) -> None:
        """Revoke a specific session by token ID. Raises 404 if not owned by user."""
        from specivo.core.exceptions import NotFoundError

        stmt = select(RefreshToken).where(
            RefreshToken.id == token_id,
            RefreshToken.user_id == user_id,
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None:
            raise NotFoundError(message=_("Session not found"))
        await session.delete(record)
        await session.flush()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _find_user(self, session: AsyncSession, normalized: str) -> User | None:
        """Find a user by login or email (both case-insensitive).

        Uses LIMIT 1 to handle the theoretical edge case where login matches
        another user's email — unique expression indexes prevent this in practice.
        """
        stmt = (
            select(User).where((func.lower(User.login) == normalized) | (func.lower(User.email) == normalized)).limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _store_refresh_token(
        self,
        session: AsyncSession,
        user_id: int,
        raw_token: str,
        device_info: str | None,
        ip_address: str | None,
        settings,
    ) -> RefreshToken:
        """Hash *raw_token* and persist a RefreshToken record."""
        record = RefreshToken(
            user_id=user_id,
            token_hash=_hash_token(raw_token),
            device_info=device_info,
            ip_address=ip_address,
            expires_at=utcnow() + timedelta(days=settings.refresh_token_expire_days),
        )
        session.add(record)
        return record
