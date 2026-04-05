"""Authentication service: login, refresh, logout, session management, password reset.

Implements:
- JWT access token generation (PyJWT, HS256, 15 min)
- Opaque refresh token generation (secrets.token_urlsafe, stored as SHA-256 hash)
- Token rotation on refresh (replay attack detection)
- Progressive account lockout on failed logins
- Session listing and targeted revocation
- Self-service password reset via email token
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import timedelta
from pathlib import Path

import jwt
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.constants import CREDENTIAL_TOKEN_ENTROPY_BYTES, JWT_ALGORITHM, REFRESH_TOKEN_ENTROPY_BYTES
from specivo.core.exceptions import AppError
from specivo.core.i18n import gettext as _
from specivo.core.utils import utcnow
from specivo.models.auth import PasswordResetToken, RefreshToken
from specivo.models.user import User
from specivo.services.auth_utils import hash_password, verify_password
from specivo.tasks.notifications import send_notification_email

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


def _make_access_token(user: User, settings, *, remember: bool = True) -> str:
    """Encode a JWT access token for *user*.

    The ``rem`` claim stores the "Remember Me" preference so that
    token refresh can carry the cookie-lifetime policy forward.
    """
    now = utcnow()
    exp = now + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": str(user.id),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": str(uuid.uuid4()),
        "rem": remember,
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


_logger = logging.getLogger(__name__)
_EMAIL_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "_shared"
_email_env = Environment(
    loader=FileSystemLoader(str(_EMAIL_TEMPLATES_DIR)),
    autoescape=True,
)


def _render_email(template_name: str, **context: object) -> str:
    template = _email_env.get_template(f"email/{template_name}")
    return template.render(**context)


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
        remember: bool = False,
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

        # 3. Block service accounts from password login
        if user.is_service_account:
            raise AppError(
                code="auth_service_account",
                message=_("Service accounts cannot log in. Use an API key instead."),
                status_code=401,
            )

        # 4. Check active lockout
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

        # Auto-assign avatar color on first login if not set
        if not user.preferences.get("avatar_color"):
            import random

            from specivo.services.settings_service import SettingsService

            try:
                palette = await SettingsService().get_avatar_palette(session)
                color = random.choice(palette)
                user.preferences = {**user.preferences, "avatar_color": color}
            except Exception:
                pass  # Non-critical — don't block login

        # 7 & 8. Generate tokens
        access_token = _make_access_token(user, settings, remember=remember)
        refresh_raw = _make_refresh_token()
        await self._store_refresh_token(session, user.id, refresh_raw, device_info, ip, settings)

        await session.flush()
        return access_token, refresh_raw

    async def refresh(
        self,
        session: AsyncSession,
        refresh_token_raw: str,
        remember: bool = True,
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
        new_access_token = _make_access_token(user, settings, remember=remember)

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
    # Password reset
    # ------------------------------------------------------------------

    async def request_password_reset(
        self,
        session: AsyncSession,
        email: str,
    ) -> None:
        """Request a password reset for the given email.

        Always succeeds silently to prevent email enumeration.
        Only sends an email if the user exists and is active.
        """
        from specivo.core.notification_templates import PASSWORD_RESET_EMAIL_SUBJECT

        settings = get_settings()
        normalized = email.strip().lower()

        # Look up user by email
        stmt = select(User).where(func.lower(User.email) == normalized)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        # Silent return for nonexistent or inactive users — no enumeration
        if user is None or user.status not in ("active", "locked"):
            return

        # Invalidate any existing unused tokens for this user
        invalidate_stmt = (
            update(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.used_at.is_(None),
            )
            .values(used_at=utcnow())
        )
        await session.execute(invalidate_stmt)

        # Generate new token
        raw_token = secrets.token_urlsafe(CREDENTIAL_TOKEN_ENTROPY_BYTES)
        expire_hours = settings.password_reset_token_expire_hours

        record = PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            expires_at=utcnow() + timedelta(hours=expire_hours),
        )
        session.add(record)
        await session.flush()

        # Build reset URL and send email
        reset_url = f"{settings.app_url.rstrip('/')}/reset-password/?token={raw_token}"
        subject = str(PASSWORD_RESET_EMAIL_SUBJECT)
        body_html = _render_email(
            "password_reset.html",
            display_name=user.display_name or user.login,
            reset_url=reset_url,
            expire_hours=expire_hours,
        )
        send_notification_email.delay(user.email, subject, body_html)

    async def reset_password_with_token(
        self,
        session: AsyncSession,
        token: str,
        new_password: str,
    ) -> None:
        """Reset a user's password using a valid reset token.

        Raises ``AppError`` if the token is invalid, expired, or already used.
        """
        token_hash = _hash_token(token)

        # Find token record
        stmt = select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()

        if record is None:
            raise AppError(
                code="password_reset_invalid",
                message=_("Invalid or expired password reset link"),
                status_code=400,
            )

        # Check if already used
        if record.used_at is not None:
            raise AppError(
                code="password_reset_invalid",
                message=_("This password reset link has already been used"),
                status_code=400,
            )

        # Check expiry
        if record.expires_at < utcnow():
            raise AppError(
                code="password_reset_invalid",
                message=_("This password reset link has expired"),
                status_code=400,
            )

        # Load user
        user_result = await session.execute(select(User).where(User.id == record.user_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            raise AppError(
                code="password_reset_invalid",
                message=_("Invalid or expired password reset link"),
                status_code=400,
            )

        # Update password
        user.password_hash = hash_password(new_password)
        user.password_changed_at = utcnow()
        user.failed_login_count = 0
        user.locked_until = None

        # If user was locked (brute-force), reactivate
        if user.status == "locked":
            user.status = "active"

        # Mark token as used
        record.used_at = utcnow()

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
