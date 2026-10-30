"""Authentication service: login, refresh, logout, session management, password reset.

Implements:
- JWT access token generation (PyJWT, HS256, 15 min)
- Opaque refresh token generation (secrets.token_urlsafe, stored as SHA-256 hash)
- Token rotation on refresh (replay attack detection); the session id survives
  rotation and is the ``sid`` claim of every access token the session mints
- Progressive account lockout on failed logins
- Session listing and targeted revocation (through ``SessionService.revoke``)
- Self-service password reset via email token
- Self-service password change for an authenticated user
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import jwt
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.config import get_settings
from specivo.core.constants import CREDENTIAL_TOKEN_ENTROPY_BYTES, JWT_ALGORITHM, REFRESH_TOKEN_ENTROPY_BYTES
from specivo.core.exceptions import AppError
from specivo.core.i18n import gettext as _
from specivo.core.utils import utcnow
from specivo.models.auth import PasswordResetToken, RefreshToken
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.schemas.sso import SSO_SESSION_MAX_AGE_HOURS
from specivo.services.auth_utils import (
    burn_password_check,
    hash_password,
    hash_upgrade_source,
    is_sso_only_user,
    rehash_if_needed,
    validate_password_policy,
    verify_password,
)
from specivo.services.session_service import SessionService
from specivo.tasks.notifications import send_notification_email

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


def _make_access_token(
    user: User,
    settings,
    *,
    remember: bool = True,
    session_id: uuid.UUID | None = None,
) -> str:
    """Encode a JWT access token for *user*.

    The ``rem`` claim stores the "Remember Me" preference so that
    token refresh can carry the cookie-lifetime policy forward. The ``sid``
    claim names the session (``refresh_tokens.session_id``), so revoking the
    session revokes the token immediately.
    """
    now = utcnow()
    exp = now + timedelta(minutes=settings.access_token_expire_minutes)
    payload: dict[str, object] = {
        "sub": str(user.id),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": str(uuid.uuid4()),
        "rem": remember,
    }
    if session_id is not None:
        payload["sid"] = str(session_id)
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
    # HTML bodies are escaped, plain-text (.txt) bodies are not.
    autoescape=select_autoescape(default_for_string=True),
)


def _render_email(template_name: str, **context: object) -> str:
    template = _email_env.get_template(f"email/{template_name}")
    return template.render(**context)


@dataclass(frozen=True, slots=True)
class LoginResult:
    """What a successful :meth:`AuthService.login` hands back to the endpoint."""

    access_token: str
    refresh_token: str
    user_id: int
    # Set when the stored hash was replaced during this sign-in: the algorithm
    # it was on before (a legacy one, or bcrypt at an outdated cost). The
    # endpoint audits it; the service has no request to audit with.
    hash_upgraded_from: str | None = None


@dataclass(frozen=True, slots=True)
class SsoSession:
    """The tokens of a session started by :meth:`AuthService.create_sso_session`."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    user_id: int
    session_id: uuid.UUID
    absolute_expires_at: datetime


#: ``secret_box`` purpose of ``RefreshToken.id_token_encrypted``.
ID_TOKEN_PURPOSE = "sso-id-token"

_sessions = SessionService()


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
    ) -> LoginResult:
        """Authenticate a user and return the tokens in a :class:`LoginResult`.

        Raises ``UnauthorizedError`` with a specific code on any failure.
        Uses a generic message for wrong credentials to prevent user enumeration.

        A hash that needs it (legacy, or bcrypt at an outdated cost) is
        upgraded once the password has verified and every account check has
        passed, inside this sign-in's transaction.
        """
        settings = get_settings()
        normalized = login_or_email.strip().lower()

        # 1. Look up by login or email (case-insensitive)
        user = await self._find_user(session, normalized)

        # With ``sso.local_login_admins_only`` (and no SSO_FORCE_LOCAL_LOGIN),
        # a password signs in administrators only: everybody else uses single
        # sign-on. Lazy import: the SSO package imports this module.
        from specivo.services.sso.auth_settings import get_login_policy

        admins_only = (await get_login_policy(session)).local_login_admins_only
        refused_by_policy = admins_only and user is not None and not user.is_anonymous and not user.is_admin

        # --- Constant-time guard: prevent user enumeration ---
        # We always try to verify a password even when the user is not found,
        # then raise the same generic error.
        # The anonymous user is not an account anybody can sign in to. It has no
        # password either, but it is refused here explicitly, and exactly like an
        # unknown login, rather than relying on that.
        # A non-admin refused by ``local_login_admins_only`` gets the same
        # answer at the same cost, before any status check, so the setting
        # reveals neither that the account exists nor its state, and does not
        # count towards its lockout.
        if user is None or user.is_system_principal or refused_by_policy:
            if refused_by_policy and user is not None:
                from specivo.services.sso.log import logger as sso_logger

                sso_logger.info("sso.local_login.refused_non_admin user_id=%s", user.id)
            # Burn one bcrypt at the configured cost, as a real check would, so
            # timing doesn't reveal existence.
            burn_password_check(password)
            raise AppError(
                code="auth_invalid_credentials",
                message=_("Invalid login or password"),
                status_code=401,
            )

        # While password sign-in is admins-only, only administrators reach this
        # point, so a specific refusal (not verified, deactivated, locked, ...)
        # would tell a stranger "this login is an administrator" and which
        # state the break-glass account is in. Every refusal then gets the
        # wrong-password answer; the real reason goes to the log only.
        # Wrong passwords still count towards the lockout: dropping it would
        # leave the most valuable accounts open to unthrottled guessing, and
        # the lockout tiers already are the per-account throttle. A locked-out
        # administrator can still sign in through SSO or reset the password.
        def refuse(code: str, message: str, *, details: dict[str, str] | None = None, burn: bool = True) -> AppError:
            if not admins_only:
                return AppError(code=code, message=message, status_code=401, details=details)
            from specivo.services.sso.log import logger as sso_logger

            sso_logger.info("sso.local_login.refused_admin user_id=%s reason=%s", user.id, code)
            if burn:
                burn_password_check(password)
            return AppError(code="auth_invalid_credentials", message=_("Invalid login or password"), status_code=401)

        # 2. Check account status
        if user.status == "pending_verification":
            raise refuse("auth_email_not_verified", _("Email address not verified"))
        if user.status == "deactivated":
            raise refuse("auth_account_deactivated", _("Account has been deactivated"))

        # 3. Block service accounts from password login
        if user.is_service_account:
            raise refuse("auth_service_account", _("Service accounts cannot log in. Use an API key instead."))

        # 4. Check active lockout
        if user.locked_until is not None and user.locked_until > utcnow():
            raise refuse(
                "auth_account_locked",
                _("Account is temporarily locked due to too many failed login attempts"),
                details={"locked_until": user.locked_until.isoformat()},
            )

        # 4. Verify password. ``verify_password`` costs one bcrypt whatever the
        # stored hash looks like (missing, legacy, unknown), so timing does not
        # tell a legacy account from a native one.
        if not verify_password(password, user.password_hash, user_id=user.id):
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
                # The password was checked already: no extra bcrypt to burn.
                raise refuse("auth_account_locked", _("Account is locked. Contact an administrator."), burn=False)
            # locked_until is in the past — clear the lock and allow login
            user.status = "active"

        # Upgrade the stored hash only now, after every account check: a locked,
        # deactivated or service account must not get its hash rewritten. It
        # commits with the rest of this sign-in.
        hash_upgraded_from = rehash_if_needed(user, password)

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
        refresh_raw = _make_refresh_token()
        record = await self._store_refresh_token(session, user.id, refresh_raw, device_info, ip, settings)
        access_token = _make_access_token(user, settings, remember=remember, session_id=record.session_id)

        await session.flush()
        return LoginResult(
            access_token=access_token,
            refresh_token=refresh_raw,
            user_id=user.id,
            hash_upgraded_from=hash_upgraded_from,
        )

    async def create_sso_session(
        self,
        session: AsyncSession,
        *,
        user: User,
        identity: UserIdentity,
        provider: SsoProvider,
        id_token: str,
        idp_sid: str | None,
        device_info: str | None = None,
        ip: str | None = None,
    ) -> SsoSession:
        """Start a new session for a user who signed in through ``provider``.

        The caller has already validated the ID token and resolved ``user``
        and ``identity`` (account status included). The refresh row records
        ``auth_method="sso"``, the identity, the IdP session id (``sid``
        claim, for back-channel logout) and the ID token encrypted for
        ``id_token_hint``. ``absolute_expires_at`` caps the session at
        ``provider.session_max_age_hours`` (at most
        ``SSO_SESSION_MAX_AGE_HOURS``), after which the user goes back to
        the IdP; ``expires_at`` never runs past it. Always new tokens, never a
        session the browser brought along.
        """
        settings = get_settings()
        now = utcnow()
        # A row saved before the cap was lowered may still hold a longer value.
        hours = min(provider.session_max_age_hours, SSO_SESSION_MAX_AGE_HOURS)
        absolute_expires_at = now + timedelta(hours=hours)

        refresh_raw = _make_refresh_token()
        record = await self._store_refresh_token(session, user.id, refresh_raw, device_info, ip, settings)
        record.auth_method = "sso"
        record.user_identity_id = identity.id
        record.idp_sid = idp_sid[:255] if idp_sid else None
        record.id_token_encrypted = secret_box.encrypt(id_token, ID_TOKEN_PURPOSE)
        record.absolute_expires_at = absolute_expires_at
        record.expires_at = min(record.expires_at, absolute_expires_at)

        access_token = _make_access_token(user, settings, remember=True, session_id=record.session_id)
        await session.flush()
        return SsoSession(
            access_token=access_token,
            refresh_token=refresh_raw,
            user_id=user.id,
            session_id=record.session_id,
            absolute_expires_at=absolute_expires_at,
        )

    async def refresh(
        self,
        session: AsyncSession,
        refresh_token_raw: str,
        remember: bool = True,
    ) -> tuple[str, str, User]:
        """Rotate a refresh token and return *(access_token, new_refresh_token_raw, user)*.

        If the token is not found (already used or never existed) we raise 401.
        Replay detection: if a token is reused after rotation, the caller should
        revoke all sessions for the user (handled by the router layer via logout_all).

        The new row inherits every session column (``session_id``,
        ``auth_method``, the SSO identity fields, ``absolute_expires_at``), so
        the session keeps its identity. Past ``absolute_expires_at`` the
        session cannot be refreshed at all. An SSO session is refused, and
        revoked for good, once its identity is unlinked or its provider is
        disabled.

        Rotation cannot outlive a concurrent revocation: the session is locked
        (``SessionService.lock_sessions``) before the old row is claimed with
        an atomic ``DELETE ... RETURNING``, so a revocation that got there
        first leaves nothing to claim and the refresh is refused. A session
        marked ``revoked_session:{sid}`` in Redis is refused too, failing
        closed when Redis is unreachable, like access-token checks.
        """
        from specivo.core.security import is_access_token_revoked

        settings = get_settings()
        token_hash = _hash_token(refresh_token_raw)
        refused = AppError(
            code="auth_refresh_expired",
            message=_("Refresh token expired or revoked"),
            status_code=401,
        )

        # 1. Find the token record
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()

        if record is None:
            raise refused

        # 2. Lock the session, then claim the old row (rotation). Zero rows
        # means a revocation deleted it after we read it.
        await _sessions.lock_sessions(session, [record.session_id])
        claimed = await session.scalar(
            delete(RefreshToken).where(RefreshToken.id == record.id).returning(RefreshToken.id)
        )
        await session.flush()
        if claimed is None:
            raise refused

        # 3. Check expiry, including the session's absolute cap
        now = utcnow()
        if record.expires_at < now or (record.absolute_expires_at is not None and record.absolute_expires_at <= now):
            raise refused

        # 4. A revoked session never rotates (fail closed if Redis is down).
        if await is_access_token_revoked(None, str(record.session_id)):
            raise refused

        # Load the associated user
        user_result = await session.execute(select(User).where(User.id == record.user_id))
        user = user_result.scalar_one_or_none()
        if user is None or user.is_system_principal or user.status not in ("active", "locked"):
            # User deleted or deactivated, or the anonymous user — refuse refresh
            raise refused

        # 5. An SSO session lives only as long as its identity and its provider:
        # unlinked (the FK set ``user_identity_id`` to NULL) or provider
        # disabled ends it. The claimed row is gone for good: commit before
        # refusing, so a rollback of the failed request cannot bring it back.
        if record.auth_method == "sso" and not await self._sso_source_active(session, record.user_identity_id):
            await _sessions.revoke(session, [record.session_id], reason="sso_source_inactive", actor_id=None)
            await session.commit()
            _logger.info(
                "sso.session.refresh_refused user_id=%s identity_id=%s reason=identity_or_provider_inactive",
                record.user_id,
                record.user_identity_id,
            )
            raise refused

        # 6. Issue new tokens
        new_refresh_raw = _make_refresh_token()
        await self._store_refresh_token(
            session,
            user.id,
            new_refresh_raw,
            record.device_info,
            record.ip_address,
            settings,
            inherit_from=record,
        )
        new_access_token = _make_access_token(user, settings, remember=remember, session_id=record.session_id)

        await session.flush()
        return new_access_token, new_refresh_raw, user

    @staticmethod
    async def _sso_source_active(session: AsyncSession, user_identity_id: int | None) -> bool:
        """The identity still exists and its provider is enabled."""
        if user_identity_id is None:
            return False
        enabled = await session.scalar(
            select(SsoProvider.enabled)
            .join(UserIdentity, UserIdentity.provider_id == SsoProvider.id)
            .where(UserIdentity.id == user_identity_id)
        )
        return enabled is True

    async def logout(
        self,
        session: AsyncSession,
        refresh_token_raw: str,
        *,
        reason: str = "logout",
    ) -> None:
        """End the session behind *refresh_token_raw*. Silent if not found."""
        await self.end_browser_session(
            session, refresh_token_raw=refresh_token_raw, access_token_raw=None, reason=reason
        )

    async def end_browser_session(
        self,
        session: AsyncSession,
        *,
        refresh_token_raw: str | None,
        access_token_raw: str | None,
        reason: str = "logout",
    ) -> None:
        """End the session a client holds, on the server side.

        The session is found from the refresh-token row behind
        *refresh_token_raw* and from the ``sid`` claim of *access_token_raw*
        (normally the same session), and revoked through
        ``SessionService.revoke``: its refresh rows are deleted and its access
        tokens are refused from the next request on. The access token's
        ``jti`` is blocklisted as well, which covers a token minted before the
        ``sid`` claim existed. Either value may be missing (an expired access
        cookie, a Bearer client without a refresh token); whatever is present
        is revoked.

        The access token's ``sid`` is only acted on when the token is still
        valid, or when it belongs to the same user as the refresh token
        presented with it. An expired token proves nothing about who holds it
        now: an old or leaked one must not let anybody end its owner's session.
        """
        from specivo.core.security import access_token_session, blocklist_access_token

        session_ids: list[uuid.UUID] = []
        refresh_owner: int | None = None
        if refresh_token_raw:
            found = await _sessions.session_for_refresh_token(session, _hash_token(refresh_token_raw))
            if found is not None:
                session_ids.append(found[0])
                refresh_owner = found[1]
        access = access_token_session(access_token_raw)
        if access is not None and (
            not access.expired or (refresh_owner is not None and access.user_id == refresh_owner)
        ):
            session_ids.append(access.session_id)
        if session_ids:
            actor = refresh_owner if refresh_owner is not None else (access.user_id if access is not None else None)
            await _sessions.revoke(session, session_ids, reason=reason, actor_id=actor)
        if access_token_raw:
            await blocklist_access_token(access_token_raw)

    async def current_session_id(
        self,
        session: AsyncSession,
        *,
        refresh_token_raw: str | None,
    ) -> uuid.UUID | None:
        """Return the session *refresh_token_raw* belongs to, or ``None``."""
        if not refresh_token_raw:
            return None
        found = await _sessions.session_for_refresh_token(session, _hash_token(refresh_token_raw))
        return found[0] if found is not None else None

    async def logout_all(
        self,
        session: AsyncSession,
        user_id: int,
    ) -> int:
        """Revoke every session of *user_id*. Returns the number of refresh rows deleted."""
        return await _sessions.revoke_user_sessions(session, user_id, reason="logout_all", actor_id=user_id)

    async def logout_other_sessions(
        self,
        session: AsyncSession,
        user_id: int,
        keep_refresh_token_raw: str | None = None,
        *,
        caller_access_token_raw: str | None = None,
        reason: str = "password_change",
    ) -> int:
        """Revoke every session of *user_id* except the caller's own.

        Sibling of :meth:`logout_all`, used after a password change: the other
        devices must lose their sessions, but the browser that performed the
        change has to stay signed in — otherwise changing a password logs you
        out of the page you changed it on.

        *keep_refresh_token_raw* is the caller's own raw refresh token (from the
        ``refresh_token`` cookie). When it is ``None`` (or unknown) — a
        Bearer-token client that never received a refresh cookie — every
        session's refresh token is revoked, as with :meth:`logout_all`; the
        access tokens of the session named by *caller_access_token_raw* are
        spared, so the caller's own token keeps working until it expires.

        Returns the number of refresh rows deleted.
        """
        from specivo.core.security import access_token_session

        keep = await self.current_session_id(session, refresh_token_raw=keep_refresh_token_raw)
        caller = access_token_session(caller_access_token_raw)
        # Only the live token the caller authenticated with is spared.
        spare = caller.session_id if keep is None and caller is not None and not caller.expired else None
        return await _sessions.revoke_user_sessions(
            session, user_id, reason=reason, keep_session_id=keep, spare_access_of=spare, actor_id=user_id
        )

    async def list_sessions(
        self,
        session: AsyncSession,
        user_id: int,
    ) -> list[tuple[RefreshToken, str | None]]:
        """Return *(refresh token, SSO provider name)* for every live session of *user_id*.

        The provider name is ``None`` for password sessions and for SSO
        sessions whose identity has since been unlinked.
        """
        stmt = (
            select(RefreshToken, SsoProvider.display_name)
            .outerjoin(UserIdentity, UserIdentity.id == RefreshToken.user_identity_id)
            .outerjoin(SsoProvider, SsoProvider.id == UserIdentity.provider_id)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.expires_at > func.now(),
            )
            .order_by(RefreshToken.created_at.desc())
        )
        result = await session.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]

    async def revoke_session(
        self,
        session: AsyncSession,
        user_id: int,
        token_id: int,
    ) -> None:
        """Revoke the session of refresh token *token_id*. Raises 404 if not owned by user."""
        from specivo.core.exceptions import NotFoundError

        stmt = select(RefreshToken.session_id).where(
            RefreshToken.id == token_id,
            RefreshToken.user_id == user_id,
        )
        session_id = await session.scalar(stmt)
        if session_id is None:
            raise NotFoundError(message=_("Session not found"))
        await _sessions.revoke(session, [session_id], reason="revoke_session", actor_id=user_id)

    # ------------------------------------------------------------------
    # Password reset
    # ------------------------------------------------------------------

    async def request_password_reset(
        self,
        session: AsyncSession,
        email: str,
    ) -> int | None:
        """Request a password reset for the given email.

        Always succeeds silently to prevent email enumeration.
        Only sends an email if the user exists and is active.
        Returns the user_id if a token was generated, None otherwise.
        """
        from specivo.core.notification_templates import PASSWORD_RESET_EMAIL_SUBJECT

        settings = get_settings()
        normalized = email.strip().lower()

        # Look up user by email
        stmt = select(User).where(func.lower(User.email) == normalized)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        # Silent return for nonexistent or inactive users, and for the anonymous
        # user, which no one may take over — no enumeration
        if user is None or user.is_system_principal or user.status not in ("active", "locked"):
            return None
        # An SSO-only account must not get a local password by email: it would
        # bypass every rule tied to signing in through the IdP. Same silent
        # answer as an unknown email; an admin can still set one deliberately.
        if await is_sso_only_user(session, user):
            _logger.info("password_reset.refused_sso_only user_id=%s", user.id)
            return None

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
        context = {
            "display_name": user.display_name or user.login,
            "reset_url": reset_url,
            "expire_hours": expire_hours,
        }
        body_html = _render_email("password_reset.html", **context)
        body_plain = _render_email("password_reset.txt", **context)
        send_notification_email.delay(user.email, subject, body_html, body_plain)
        return user.id

    async def reset_password_with_token(
        self,
        session: AsyncSession,
        token: str,
        new_password: str,
    ) -> int:
        """Reset a user's password using a valid reset token.

        Returns the user_id of the user whose password was reset.
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
        # An SSO-only account (a token issued before its identity was
        # connected) is refused like an invalid link: email reset never gives
        # it a local password.
        if user is None or user.is_system_principal or await is_sso_only_user(session, user):
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
        # Somebody completing an email reset picked this password themselves,
        # so a forced change has already happened. Leaving the flag set would
        # demand a second change the moment they sign in.
        user.must_change_password = False

        # If user was locked (brute-force), reactivate
        if user.status == "locked":
            user.status = "active"

        # Mark token as used
        record.used_at = utcnow()

        # A reset is the compromise-recovery path: every session that existed
        # before it ends now, access tokens included.
        await _sessions.revoke_user_sessions(session, user.id, reason="password_reset", actor_id=user.id)

        await session.flush()
        return user.id

    # ------------------------------------------------------------------
    # Self-service password change
    # ------------------------------------------------------------------

    async def change_password(
        self,
        session: AsyncSession,
        user: User,
        current_password: str,
        new_password: str,
    ) -> str | None:
        """Change *user*'s password after verifying *current_password*.

        Returns the algorithm the stored hash is upgraded from when it was a
        legacy or outdated one (see ``hash_upgrade_source``), else ``None``, so the
        caller can audit it.

        Raises ``AppError`` (400) when the account has no password to change,
        when the current password is wrong, or when the new password repeats
        the current one; ``AppError`` (422) when the new password fails the
        password policy.

        The caller is responsible for revoking the user's other sessions and
        for writing the audit trail.
        """
        # Accounts without a password (service accounts authenticate by API
        # key, SSO-only users through their identity provider) have nothing to
        # verify against — reject before touching bcrypt.
        if user.is_service_account:
            raise AppError(
                code="password_change_unavailable",
                message=_("This account has no password. It authenticates with an API key."),
                status_code=400,
            )
        if not user.password_hash:
            raise AppError(
                code="password_change_unavailable",
                message=_(
                    "This account has no password. You sign in through your identity provider, "
                    "so there is no password to change here."
                ),
                status_code=400,
            )

        # Verify the current password before any other check, so no other
        # failure mode can be reached without knowing it.
        if not verify_password(current_password, user.password_hash, user_id=user.id):
            raise AppError(
                code="password_current_invalid",
                message=_("Current password is incorrect"),
                status_code=400,
                field="current_password",
            )

        validate_password_policy(new_password)

        if new_password == current_password:
            raise AppError(
                code="password_unchanged",
                message=_("The new password must be different from the current one"),
                status_code=400,
                field="new_password",
            )

        # The current password verified. If it was stored under a legacy or
        # outdated hash, record that it is being upgraded — here by the new
        # password replacing it — only now that nothing else can reject the
        # change, so a rejected change leaves the hash exactly as it was.
        upgraded_from = hash_upgrade_source(user.password_hash)
        user.password_hash = hash_password(new_password)
        user.password_changed_at = utcnow()
        # Mirror the token-reset path: a successful password change clears the
        # brute-force counters. status="locked" is not handled here because a
        # locked account cannot authenticate with a JWT and so cannot reach
        # this code path at all.
        user.failed_login_count = 0
        user.locked_until = None
        # The password is now one the owner chose, which is the whole point of
        # the flag. This is the way out of a forced change.
        user.must_change_password = False

        await session.flush()
        return upgraded_from

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
        *,
        inherit_from: RefreshToken | None = None,
    ) -> RefreshToken:
        """Hash *raw_token* and persist a RefreshToken record.

        Without *inherit_from* the row starts a new password session. With it
        (rotation) the row continues that session: every session column is
        copied, and ``expires_at`` never runs past ``absolute_expires_at``.
        """
        expires_at = utcnow() + timedelta(days=settings.refresh_token_expire_days)
        record = RefreshToken(
            user_id=user_id,
            token_hash=_hash_token(raw_token),
            device_info=device_info,
            ip_address=ip_address,
            session_id=uuid.uuid4(),
            auth_method="password",
        )
        if inherit_from is not None:
            record.session_id = inherit_from.session_id
            record.auth_method = inherit_from.auth_method
            record.user_identity_id = inherit_from.user_identity_id
            record.idp_sid = inherit_from.idp_sid
            record.id_token_encrypted = inherit_from.id_token_encrypted
            record.absolute_expires_at = inherit_from.absolute_expires_at
            if inherit_from.absolute_expires_at is not None:
                expires_at = min(expires_at, inherit_from.absolute_expires_at)
        record.expires_at = expires_at
        session.add(record)
        return record
