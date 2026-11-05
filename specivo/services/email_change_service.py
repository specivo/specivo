"""Changing an account's email address: by the person (confirmed by link) or by an administrator.

**Self-service** (the profile page and ``/api/v1/auth/me/email/`` both call
:class:`EmailChangeService`):

1. :meth:`EmailChangeService.request_change` — the person names the new
   address and proves it is them: the current password, or, for an account
   without one that signs in through single sign-on, a fresh sign-in at the
   identity provider (the ``reauth`` SSO flow records a marker in Redis for
   this user and browser session, valid :data:`REAUTH_TTL_SECONDS`; see
   :func:`mark_recent_reauth`). The address is checked and normalised like
   every other email address (``EmailStr``) and may not be the current one.
   A link is sent to the new address; the old address stays the account's
   until the link is used.
2. :meth:`EmailChangeService.confirm` — the link holder (signed in or not:
   the token is the proof) confirms, the address is swapped and marked
   verified, the old address gets a notice, the person's other sessions end,
   pending password resets are dropped and the change is audited.

Only the latest link works: a new request, a resend or a cancel ends the
previous one. Links live :data:`EMAIL_CHANGE_TOKEN_HOURS`; only the SHA-256
of the token is stored (:class:`specivo.models.auth.EmailChangeToken`).

**An address another account uses** gets the same answer as any other: the
pending change is recorded and one email is queued, so the response, the
pending state and the work done look the same. The email is not a link but a
note to that address that someone tried to use it. Uniqueness is checked
again at confirmation.

**Rate limits** (Redis sliding window, like the auth routes):
:data:`ACCOUNT_RATE_LIMIT` per account for every request and resend, wrong
passwords included, and :data:`ADDRESS_RATE_LIMIT` per target address.

**Refused:** an account a provider's SCIM manages (its address belongs to the
identity provider), service accounts, and accounts with neither a password
nor a connected identity.

**Administrators** change an address directly with
:meth:`EmailChangeService.admin_set_email` (admin web page and API): set and
marked verified, audited, with a notice to both addresses.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import EmailStr, TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.constants import CREDENTIAL_TOKEN_ENTROPY_BYTES
from specivo.core.exceptions import AppError, ConflictError, ValidationError
from specivo.core.i18n import activate, get_locale
from specivo.core.i18n import gettext as _
from specivo.core.rate_limit import RateLimiter
from specivo.core.utils import utcnow
from specivo.models.auth import EmailChangeToken, PasswordResetToken
from specivo.models.scim import ScimResource
from specivo.models.sso import UserIdentity
from specivo.models.user import User
from specivo.services.anonymous_user_service import refuse_system_user
from specivo.services.auth_utils import verify_password
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService
from specivo.services.session_service import SessionService
from specivo.tasks.notifications import send_notification_email

__all__ = [
    "ACCOUNT_RATE_LIMIT",
    "ADDRESS_RATE_LIMIT",
    "EMAIL_CHANGE_TOKEN_HOURS",
    "REAUTH_TTL_SECONDS",
    "EmailChangeResult",
    "EmailChangeService",
    "EmailChangeState",
    "PendingEmailChange",
    "email_change_error_message",
    "has_recent_reauth",
    "mark_recent_reauth",
    "mask_email",
]

logger = logging.getLogger("specivo.auth.email_change")

#: How long a confirmation link is valid.
EMAIL_CHANGE_TOKEN_HOURS = 24
#: How long a fresh sign-in at the identity provider unlocks a request.
REAUTH_TTL_SECONDS = 300
#: (bucket, requests, window seconds): requests and resends per account.
ACCOUNT_RATE_LIMIT = ("email_change_account", 5, 3600)
#: (bucket, requests, window seconds): requests and resends per target address.
ADDRESS_RATE_LIMIT = ("email_change_address", 3, 3600)
#: ``reason`` of the sessions a confirmed change ends.
EMAIL_CHANGE_REVOKE_REASON = "email_change"

_REAUTH_KEY = "email_change_reauth:{user_id}:{session_id}"
_EMAIL_ADAPTER: TypeAdapter[str] = TypeAdapter(EmailStr)
_EMAIL_MAX_LENGTH = 255

_EMAIL_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "_shared"
_email_env = Environment(
    loader=FileSystemLoader(str(_EMAIL_TEMPLATES_DIR)),
    autoescape=select_autoescape(default_for_string=True),
)

_sessions = SessionService()
_audit = SecurityAuditService()


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingEmailChange:
    """A requested change waiting for its link to be used."""

    new_email: str
    requested_at: datetime
    expires_at: datetime

    @property
    def expired(self) -> bool:
        return self.expires_at <= utcnow()


@dataclass(frozen=True, slots=True)
class EmailChangeState:
    """What the profile and ``GET /auth/me/email/`` show."""

    email: str
    pending: PendingEmailChange | None
    #: A provider's SCIM manages the account: the address is changed there.
    managed_by_idp: bool
    #: The account has no password: a fresh sign-in at the IdP proves it is them.
    requires_reauth: bool
    #: That fresh sign-in happened in this session within the last minutes.
    reauthenticated: bool
    #: Neither a password nor a connected identity (or a service account).
    unavailable: bool


@dataclass(frozen=True, slots=True)
class EmailChangeResult:
    """A confirmed change."""

    user_id: int
    old_email: str
    new_email: str
    revoked_sessions: int


# ---------------------------------------------------------------------------
# Errors and messages
# ---------------------------------------------------------------------------


def email_change_error_message(code: str | None) -> str | None:
    """The translated message for an email change error ``code``; a generic one for unknown codes."""
    if not code:
        return None
    messages = {
        "email_change_unavailable": _("This account cannot change its email address here."),
        "email_change_managed_by_idp": _(
            "Your email address is managed by your identity provider. Change it at your identity provider."
        ),
        "email_change_reauth_required": _(
            "Sign in again with your identity provider, then request the change within five minutes."
        ),
        "email_change_password_invalid": _("Current password is incorrect"),
        "email_change_address_invalid": _("Enter a valid email address."),
        "email_change_same_address": _("This is already your email address."),
        "email_change_no_pending": _("There is no pending email change."),
        "rate_limit_exceeded": _("Too many email change requests. Please try again later."),
        "email_change_link_invalid": _("This confirmation link is not valid."),
        "email_change_link_used": _("This confirmation link has already been used."),
        "email_change_link_superseded": _(
            "This confirmation link is no longer valid. Use the most recent link, or request the change again."
        ),
        "email_change_link_expired": _("This confirmation link has expired. Request the change again."),
        "email_change_address_taken": _(
            "This email address is now used by another account. Your address was not changed."
        ),
    }
    return messages.get(code) or _("The email address could not be changed. Please try again.")


def _error(code: str, status_code: int, *, field: str | None = None) -> AppError:
    message = email_change_error_message(code) or code
    return AppError(code=code, message=message, status_code=status_code, field=field)


def _rate_limited(retry_after: int) -> AppError:
    return AppError(
        code="rate_limit_exceeded",
        message=email_change_error_message("rate_limit_exceeded") or "",
        status_code=429,
        details={"retry_after": retry_after},
        headers={"Retry-After": str(retry_after)},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _address_bucket(address: str) -> str:
    """The rate-limit identifier of an address: a hash, so no address is kept in Redis keys."""
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()[:32]


def normalize_email(raw: str) -> str:
    """``raw`` checked and normalised like every other address (``EmailStr``); 422 when invalid."""
    value = (raw or "").strip()
    try:
        if len(value) > _EMAIL_MAX_LENGTH:
            raise ValueError("too long")
        return _EMAIL_ADAPTER.validate_python(value)
    except (PydanticValidationError, ValueError):
        raise ValidationError(
            message=email_change_error_message("email_change_address_invalid") or "", field="new_email"
        ) from None


def mask_email(address: str) -> str:
    """``n…@example.com``: enough for the owner to recognise, not to read off."""
    local, _sep, domain = address.partition("@")
    return f"{local[:1]}…@{domain}" if domain else "…"


def _url(path: str) -> str:
    settings = get_settings()
    return f"{settings.app_url.rstrip('/')}{settings.stealth_prefix.rstrip('/')}{path}"


@contextmanager
def _language(language: str | None) -> Iterator[None]:
    """Translate in ``language`` (the recipient's), then restore the request's locale."""
    previous = get_locale()
    activate(language or get_settings().default_language)
    try:
        yield
    finally:
        activate(previous)


def _brand() -> str:
    from specivo.web.deps import get_brand_name

    return get_brand_name()


def _send(
    to: str,
    language: str | None,
    *,
    subject: str,
    name: str,
    paragraphs: list[str],
    action_url: str | None = None,
    action_label: str | None = None,
    action_after: int = 1,
) -> None:
    """Render the account notice (HTML and text) and queue it, as the password reset mail is."""
    context: dict[str, Any] = {
        "greeting": _("Hi %(name)s,") % {"name": name},
        "paragraphs": paragraphs,
        "action_url": action_url,
        "action_label": action_label,
        "action_after": action_after,
    }
    body_html = _email_env.get_template("email/account_notice.html").render(**context)
    body_plain = _email_env.get_template("email/account_notice.txt").render(**context)
    send_notification_email.delay(to, subject, body_html, body_plain)


def _display_name(user: User) -> str:
    return user.display_name or user.login


# -- the recent re-authentication marker (SSO-only accounts) -----------------


async def mark_recent_reauth(user_id: int, session_id: uuid.UUID) -> bool:
    """Record that ``user_id`` signed in again at the IdP in ``session_id``; ``False`` without Redis."""
    from specivo.core.redis import get_redis

    try:
        redis = await get_redis()
        await redis.set(_REAUTH_KEY.format(user_id=user_id, session_id=session_id), "1", ex=REAUTH_TTL_SECONDS)
    except Exception:
        logger.warning("email_change.reauth_mark_failed user_id=%s", user_id, exc_info=True)
        return False
    return True


async def has_recent_reauth(user_id: int, session_id: uuid.UUID | None) -> bool:
    """Whether ``user_id`` signed in again in ``session_id`` recently (fails closed without Redis)."""
    if session_id is None:
        return False
    from specivo.core.redis import get_redis

    try:
        redis = await get_redis()
        return bool(await redis.exists(_REAUTH_KEY.format(user_id=user_id, session_id=session_id)))
    except Exception:
        logger.warning("email_change.reauth_check_failed user_id=%s", user_id, exc_info=True)
        return False


async def _consume_reauth(user_id: int, session_id: uuid.UUID | None) -> None:
    if session_id is None:
        return
    from specivo.core.redis import get_redis

    try:
        redis = await get_redis()
        await redis.delete(_REAUTH_KEY.format(user_id=user_id, session_id=session_id))
    except Exception:
        logger.warning("email_change.reauth_consume_failed user_id=%s", user_id, exc_info=True)


async def _check_rate(limit: tuple[str, int, int], identifier: str) -> None:
    if not get_settings().rate_limit_enabled:
        return
    prefix, max_requests, window = limit
    allowed, _remaining, retry_after = await RateLimiter(prefix, max_requests, window).check(identifier)
    if not allowed:
        raise _rate_limited(retry_after)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class EmailChangeService:
    """Stateless; all state lives in the DB session and Redis."""

    # -- reads --------------------------------------------------------------

    async def managed_by_idp(self, db: AsyncSession, user_id: int) -> bool:
        """A provider's SCIM manages this account (also after a SCIM delete)."""
        found = await db.scalar(
            select(ScimResource.id).where(ScimResource.user_id == user_id, ScimResource.resource_type == "User")
        )
        return found is not None

    async def _has_identity(self, db: AsyncSession, user_id: int) -> bool:
        return (await db.scalar(select(UserIdentity.id).where(UserIdentity.user_id == user_id).limit(1))) is not None

    async def pending(self, db: AsyncSession, user_id: int) -> PendingEmailChange | None:
        """The live (not used, not invalidated) change of ``user_id``, expired or not."""
        row = await self._live_row(db, user_id)
        if row is None:
            return None
        return PendingEmailChange(new_email=row.new_email, requested_at=row.created_at, expires_at=row.expires_at)

    async def state(self, db: AsyncSession, user: User, *, session_id: uuid.UUID | None) -> EmailChangeState:
        """Everything the profile shows about changing ``user``'s address."""
        managed = await self.managed_by_idp(db, user.id)
        requires_reauth = not user.password_hash
        unavailable = user.is_service_account or (requires_reauth and not await self._has_identity(db, user.id))
        return EmailChangeState(
            email=user.email,
            pending=None if managed else await self.pending(db, user.id),
            managed_by_idp=managed,
            requires_reauth=requires_reauth and not unavailable,
            reauthenticated=requires_reauth and await has_recent_reauth(user.id, session_id),
            unavailable=unavailable,
        )

    async def _live_row(self, db: AsyncSession, user_id: int) -> EmailChangeToken | None:
        return await db.scalar(
            select(EmailChangeToken).where(
                EmailChangeToken.user_id == user_id,
                EmailChangeToken.confirmed_at.is_(None),
                EmailChangeToken.invalidated_at.is_(None),
            )
        )

    # -- request -------------------------------------------------------------

    async def _refuse_unchangeable(self, db: AsyncSession, user: User) -> None:
        refuse_system_user(user)
        if user.is_service_account:
            raise _error("email_change_unavailable", 400)
        if await self.managed_by_idp(db, user.id):
            raise _error("email_change_managed_by_idp", 403)

    async def _fail(self, db: AsyncSession, user_id: int | None, reason: str, exc: AppError) -> AppError:
        """Audit a refused request or confirmation and commit it, so the caller's rollback keeps the row."""
        try:
            await _audit.log_event(db, AuditEvent.EMAIL_CHANGE_FAILED, user_id=user_id, details={"reason": reason})
            await db.commit()
        except Exception:
            logger.warning("Security audit logging failed for an email change refusal", exc_info=True)
        return exc

    async def request_change(
        self,
        db: AsyncSession,
        user: User,
        *,
        new_email: str,
        current_password: str | None,
        session_id: uuid.UUID | None,
    ) -> PendingEmailChange:
        """Start a change of ``user``'s address to ``new_email`` and send the link (see the module docstring).

        Raises ``AppError``: ``email_change_unavailable`` / ``_managed_by_idp``
        / ``_reauth_required`` (the account cannot, or not now),
        ``email_change_password_invalid`` (400, ``current_password``),
        ``validation_error`` (422, ``new_email``), ``email_change_same_address``
        (400) and ``rate_limit_exceeded`` (429).
        """
        await self._refuse_unchangeable(db, user)
        # Every attempt counts, a wrong password too: a stolen session must not
        # become a way to guess the password.
        await _check_rate(ACCOUNT_RATE_LIMIT, str(user.id))

        reauth_used = False
        if user.password_hash:
            if not verify_password(current_password or "", user.password_hash, user_id=user.id):
                raise await self._fail(
                    db,
                    user.id,
                    "email_change_password_invalid",
                    _error("email_change_password_invalid", 400, field="current_password"),
                )
        elif await self._has_identity(db, user.id):
            if not await has_recent_reauth(user.id, session_id):
                raise await self._fail(
                    db, user.id, "email_change_reauth_required", _error("email_change_reauth_required", 403)
                )
            reauth_used = True
        else:
            raise _error("email_change_unavailable", 400)

        address = normalize_email(new_email)
        if address.lower() == user.email.lower():
            raise _error("email_change_same_address", 400, field="new_email")
        await _check_rate(ADDRESS_RATE_LIMIT, _address_bucket(address))

        pending = await self._issue(db, user, address)
        if reauth_used:
            await _consume_reauth(user.id, session_id)
        return pending

    async def resend(self, db: AsyncSession, user: User) -> PendingEmailChange:
        """Send a new link for the pending change; the previous link stops working.

        Raises ``email_change_no_pending`` (404) without a pending change, and
        the rate limits of a request.
        """
        await self._refuse_unchangeable(db, user)
        await _check_rate(ACCOUNT_RATE_LIMIT, str(user.id))
        row = await self._live_row(db, user.id)
        if row is None:
            raise _error("email_change_no_pending", 404)
        await _check_rate(ADDRESS_RATE_LIMIT, _address_bucket(row.new_email))
        return await self._issue(db, user, row.new_email, resend=True)

    async def cancel(self, db: AsyncSession, user: User) -> bool:
        """End the pending change, if any; its link stops working. Returns whether there was one."""
        refuse_system_user(user)
        await db.execute(select(User.id).where(User.id == user.id).with_for_update())
        ended = await self._invalidate_live(db, user.id, "cancelled")
        if ended:
            logger.info("email_change.cancelled user_id=%s", user.id)
        return ended > 0

    async def _invalidate_live(self, db: AsyncSession, user_id: int, reason: str) -> int:
        result = await db.execute(
            update(EmailChangeToken)
            .where(
                EmailChangeToken.user_id == user_id,
                EmailChangeToken.confirmed_at.is_(None),
                EmailChangeToken.invalidated_at.is_(None),
            )
            .values(invalidated_at=utcnow(), invalidation_reason=reason)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def _address_owner(self, db: AsyncSession, address: str, *, except_user_id: int) -> User | None:
        return await db.scalar(
            select(User).where(func.lower(User.email) == address.lower(), User.id != except_user_id).limit(1)
        )

    async def _issue(self, db: AsyncSession, user: User, address: str, *, resend: bool = False) -> PendingEmailChange:
        """Replace the live row with a new one and queue exactly one email.

        Both branches do the same work: end the live row, look the address up,
        insert a row with a fresh token, render one mail and queue it. The
        token of an address in use is never sent anywhere, so that row can only
        expire, be replaced or be cancelled.
        """
        # One request at a time per account: the partial unique index allows
        # one live row, and this lock keeps two requests from racing for it.
        await db.execute(select(User.id).where(User.id == user.id).with_for_update())
        await self._invalidate_live(db, user.id, "superseded")
        owner = await self._address_owner(db, address, except_user_id=user.id)

        raw_token = secrets.token_urlsafe(CREDENTIAL_TOKEN_ENTROPY_BYTES)
        now = utcnow()
        row = EmailChangeToken(
            user_id=user.id,
            new_email=address,
            token_hash=_hash_token(raw_token),
            expires_at=now + timedelta(hours=EMAIL_CHANGE_TOKEN_HOURS),
        )
        db.add(row)
        await db.flush()

        brand = _brand()
        if owner is None:
            with _language(user.language):
                _send(
                    address,
                    user.language,
                    subject=_("Confirm your new email address for %(brand)s") % {"brand": brand},
                    name=_display_name(user),
                    paragraphs=[
                        _(
                            "Someone, hopefully you, asked to use this address for the %(brand)s account "
                            "%(login)s. Confirm the change to make it the account's email address."
                        )
                        % {"brand": brand, "login": user.login},
                        _(
                            "This link expires in %(hours)s hours. Only the most recent link works. Until you "
                            "confirm, the account keeps its current address."
                        )
                        % {"hours": EMAIL_CHANGE_TOKEN_HOURS},
                        _("If you did not ask for this, ignore this email. Nothing changes."),
                    ],
                    action_url=_url(f"/confirm-email/?token={raw_token}"),
                    action_label=_("Confirm email address"),
                )
        else:
            with _language(owner.language):
                _send(
                    address,
                    owner.language,
                    subject=_("Someone tried to use your email address on %(brand)s") % {"brand": brand},
                    name=_display_name(owner),
                    paragraphs=[
                        _(
                            "Someone tried to use this address for another %(brand)s account. This address "
                            "already belongs to your account, so nothing was changed."
                        )
                        % {"brand": brand},
                        _(
                            "If it was you, sign in to the account that already uses this address. If you have "
                            "forgotten its password, reset it here:"
                        ),
                        _("If you do not recognise this, you can ignore this email."),
                    ],
                    action_url=_url("/forgot-password/"),
                    action_label=_("Reset password"),
                    action_after=2,
                )

        try:
            await _audit.log_event(
                db,
                AuditEvent.EMAIL_CHANGE_REQUESTED,
                user_id=user.id,
                details={"new_email": address, "address_in_use": owner is not None, "resend": resend},
            )
        except Exception:
            logger.warning("Security audit logging failed for an email change request", exc_info=True)
        logger.info("email_change.requested user_id=%s resend=%s", user.id, resend)
        return PendingEmailChange(new_email=address, requested_at=now, expires_at=row.expires_at)

    # -- confirm -------------------------------------------------------------

    async def _row_for_token(self, db: AsyncSession, raw_token: str, *, lock: bool = False) -> EmailChangeToken | None:
        if not raw_token or len(raw_token) > 256:
            return None
        stmt = select(EmailChangeToken).where(EmailChangeToken.token_hash == _hash_token(raw_token))
        if lock:
            stmt = stmt.with_for_update()
        return await db.scalar(stmt)

    @staticmethod
    def _link_problem(row: EmailChangeToken | None) -> str | None:
        if row is None:
            return "email_change_link_invalid"
        if row.confirmed_at is not None:
            return "email_change_link_used"
        if row.invalidated_at is not None:
            return "email_change_link_superseded"
        if row.expires_at <= utcnow():
            return "email_change_link_expired"
        return None

    async def inspect(self, db: AsyncSession, raw_token: str) -> tuple[str | None, str | None]:
        """For the confirm page: ``(problem code or None, the new address)``; changes nothing."""
        row = await self._row_for_token(db, raw_token)
        problem = self._link_problem(row)
        if problem is None and row is not None:
            user = await db.get(User, row.user_id)
            if user is None or user.is_system_principal or user.status not in ("active", "locked"):
                problem = "email_change_link_invalid"
        return problem, (row.new_email if row is not None and problem is None else None)

    async def confirm(
        self,
        db: AsyncSession,
        raw_token: str,
        *,
        caller_user_id: int | None = None,
        caller_session_id: uuid.UUID | None = None,
    ) -> EmailChangeResult:
        """Use the link: swap the address and do everything that goes with it (module docstring).

        The caller's session survives when the request carries a session of
        the same user; otherwise every session of the account ends. Raises
        ``email_change_link_*`` (400) for an unknown, used, superseded,
        cancelled or expired link, and ``email_change_address_taken`` (409)
        when another account took the address meanwhile.
        """
        row = await self._row_for_token(db, raw_token, lock=True)
        problem = self._link_problem(row)
        user: User | None = None
        if row is not None:
            user = await db.scalar(select(User).where(User.id == row.user_id).with_for_update())
            if problem is None and (
                user is None or user.is_system_principal or user.status not in ("active", "locked")
            ):
                problem = "email_change_link_invalid"
            if problem is None and user is not None and await self.managed_by_idp(db, user.id):
                problem = "email_change_managed_by_idp"
        if problem is not None:
            exc = _error(problem, 403 if problem == "email_change_managed_by_idp" else 400)
            if row is None:
                # An unknown token names nobody: nothing worth an audit row.
                raise exc
            raise await self._fail(db, row.user_id, problem, exc)
        assert row is not None and user is not None

        new_email = row.new_email
        if await self._address_owner(db, new_email, except_user_id=user.id) is not None:
            row.invalidated_at = utcnow()
            row.invalidation_reason = "address_taken"
            raise await self._fail(db, user.id, "email_change_address_taken", _error("email_change_address_taken", 409))

        old_email = user.email
        now = utcnow()
        savepoint = await db.begin_nested()
        try:
            user.email = new_email
            user.email_verified_at = now
            row.confirmed_at = now
            await db.flush()
        except IntegrityError:
            # Another account took the address between the check and the write.
            await savepoint.rollback()
            row.invalidated_at = utcnow()
            row.invalidation_reason = "address_taken"
            raise await self._fail(
                db, user.id, "email_change_address_taken", _error("email_change_address_taken", 409)
            ) from None
        if savepoint.is_active:
            await savepoint.commit()

        # A reset link mailed before the change must not outlive it.
        await db.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
            .values(used_at=now)
        )
        keep = caller_session_id if caller_user_id == user.id else None
        revoked = await _sessions.revoke_user_sessions(
            db, user.id, reason=EMAIL_CHANGE_REVOKE_REASON, keep_session_id=keep, actor_id=user.id
        )

        brand = _brand()
        with _language(user.language):
            _send(
                old_email,
                user.language,
                subject=_("Your %(brand)s email address was changed") % {"brand": brand},
                name=_display_name(user),
                paragraphs=[
                    _("Your %(brand)s email was changed to %(address)s.")
                    % {"brand": brand, "address": mask_email(new_email)},
                    _(
                        "If you did not do this, reset your password straight away and contact an administrator "
                        "of %(brand)s. Password reset emails now go to the new address, so an administrator may "
                        "have to restore your address first."
                    )
                    % {"brand": brand},
                ],
                action_url=_url("/forgot-password/"),
                action_label=_("Reset password"),
            )

        try:
            await _audit.log_event(
                db,
                AuditEvent.EMAIL_CHANGED,
                user_id=user.id,
                details={
                    "by": "user",
                    "target_user_id": user.id,
                    "target_login": user.login,
                    "old_email": old_email,
                    "new_email": new_email,
                    "revoked_sessions": revoked,
                },
            )
        except Exception:
            logger.warning("Security audit logging failed for an email change", exc_info=True)
        logger.info("email_change.confirmed user_id=%s revoked_sessions=%s", user.id, revoked)
        return EmailChangeResult(user_id=user.id, old_email=old_email, new_email=new_email, revoked_sessions=revoked)

    # -- administrators -------------------------------------------------------

    async def admin_set_email(self, db: AsyncSession, target: User, new_email: str, *, admin: User) -> tuple[str, str]:
        """Set ``target``'s address directly and mark it verified; returns ``(old, new)``.

        The change is audited and both addresses get a notice. A pending
        self-service change and pending password resets end. Raises 422 for
        an invalid address, 400 when it is the current one and 409 when
        another account uses it.
        """
        refuse_system_user(target)
        address = normalize_email(new_email)
        if address == target.email:
            raise _error("email_change_same_address", 400, field="email")
        await db.execute(select(User.id).where(User.id == target.id).with_for_update())
        if await self._address_owner(db, address, except_user_id=target.id) is not None:
            raise ConflictError(message=_("A user with this email already exists"))

        old_email = target.email
        now = utcnow()
        savepoint = await db.begin_nested()
        try:
            target.email = address
            target.email_verified_at = now
            await db.flush()
        except IntegrityError:
            await savepoint.rollback()
            raise ConflictError(message=_("A user with this email already exists")) from None
        if savepoint.is_active:
            await savepoint.commit()
        await self._invalidate_live(db, target.id, "cancelled")
        await db.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.user_id == target.id, PasswordResetToken.used_at.is_(None))
            .values(used_at=now)
        )

        brand = _brand()
        with _language(target.language):
            for to in dict.fromkeys([old_email, address]):
                _send(
                    to,
                    target.language,
                    subject=_("Your %(brand)s email address was changed") % {"brand": brand},
                    name=_display_name(target),
                    paragraphs=[
                        _(
                            "An administrator changed the email address of your %(brand)s account "
                            "%(login)s to %(address)s."
                        )
                        % {"brand": brand, "login": target.login, "address": mask_email(address)},
                        _("If you did not expect this, contact an administrator of %(brand)s.") % {"brand": brand},
                    ],
                )

        try:
            await _audit.log_event(
                db,
                AuditEvent.EMAIL_CHANGED,
                user_id=admin.id,
                details={
                    "by": "admin",
                    "target_user_id": target.id,
                    "target_login": target.login,
                    "old_email": old_email,
                    "new_email": address,
                },
            )
        except Exception:
            logger.warning("Security audit logging failed for an admin email change", exc_info=True)
        logger.info("email_change.admin user_id=%s admin_id=%s", target.id, admin.id)
        return old_email, address
