"""Account lifecycle: deactivation (deprovisioning) and reactivation.

:meth:`UserService.deactivate` is the single path that switches an account
off, for every user (password, SSO, service account): the admin API, the
admin user page and a refused SSO sign-in with ``deactivate_on_access_loss``
all go through it. In the caller's transaction it

1. sets ``status="deactivated"`` and ``deactivation_reason`` (who may undo
   it: SCIM only reactivates what SCIM deactivated);
2. revokes every session of the account (``SessionService``): the refresh
   rows are deleted and the access tokens already issued are refused on the
   next request;
3. switches every API key of the account off (``is_active=False``). Keys are
   kept, not deleted, so the audit trail of what they did stays readable;
4. records one ``user.deprovisioned`` log line with the counts
   (:func:`record_user_deprovisioned`).

:meth:`UserService.reactivate` sets the account active again and does
**not** switch the keys back on: they were turned off on purpose, and the
owner (or an admin) re-enables or replaces them explicitly.

The last active admin is never deactivated (:class:`LastActiveAdminError`):
nobody would be left to undo it from the UI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError
from specivo.core.i18n import gettext as _
from specivo.models.auth import ApiKey
from specivo.models.user import User
from specivo.services.anonymous_user_service import refuse_system_user
from specivo.services.api_key_service import ApiKeyService
from specivo.services.session_service import SessionService

__all__ = [
    "DEACTIVATION_REVOKE_REASON",
    "LAST_ADMIN_LOCK_KEY",
    "DeactivationResult",
    "LastActiveAdminError",
    "UserService",
    "another_active_admin_exists",
    "record_user_deprovisioned",
    "record_user_reactivated",
]

logger = logging.getLogger("specivo.auth.users")

#: ``reason`` recorded on the sessions a deactivation revokes.
DEACTIVATION_REVOKE_REASON = "user_deactivated"

#: Transaction-scoped advisory lock serialising the last-admin guard. Every
#: check that may end in removing an administrator (deactivation, SSO admin
#: demotion) takes it first, so two transactions removing two different
#: admins at once cannot each see the other one still active. Single-bigint
#: key like ``ANONYMOUS_ACCESS_LOCK_KEY`` (a different value); the per-session
#: locks use the two-key form and never collide with it.
LAST_ADMIN_LOCK_KEY = 0x5350_4C41_444D_0001


class LastActiveAdminError(AppError):
    """Deactivating this account would leave no active administrator."""

    def __init__(self) -> None:
        super().__init__(
            code="last_active_admin",
            message=_("This is the last active administrator. Make another user an administrator first."),
            status_code=409,
        )


@dataclass(frozen=True, slots=True)
class DeactivationResult:
    """What a deactivation did, for the response and the audit trail."""

    sessions_revoked: int
    api_keys_deactivated: int


async def another_active_admin_exists(db: AsyncSession, user_id: int) -> bool:
    """Some other active, human administrator exists (the last-admin guard).

    Takes :data:`LAST_ADMIN_LOCK_KEY` first and holds it until the caller's
    transaction ends, so the check and the demotion or deactivation that
    follows it are atomic with respect to every other guard check. A second
    transaction waits here, then (read committed) sees the first one's change.
    """
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LAST_ADMIN_LOCK_KEY})
    stmt = select(User.id).where(
        User.id != user_id,
        User.is_admin.is_(True),
        User.status == "active",
        User.is_service_account.is_(False),
        User.is_anonymous.is_(False),
    )
    return (await db.execute(stmt.limit(1))).first() is not None


def record_user_deprovisioned(
    *,
    user_id: int,
    reason: str,
    actor_id: int | None,
    sessions_revoked: int,
    api_keys_deactivated: int,
    provider_slug: str | None = None,
    issuer: str | None = None,
    subject: str | None = None,
) -> None:
    """Record one deactivation: the single place for its log line and its audit event.

    ``reason`` is ``admin`` for an administrator's action and
    ``sso_access_lost`` for a sign-in refused by a provider's access policy
    (which also names the provider and the subject). ``actor_id`` is the
    administrator, ``None`` for the system. The ``USER_DEPROVISIONED`` audit
    event goes into the request's audit buffer (or an ``audit_scope``).
    """
    from specivo.services.sso import events

    events.user_deprovisioned(
        user_id=user_id,
        actor_id=actor_id,
        reason=reason,
        provider_slug=provider_slug,
        issuer=issuer,
        subject=subject,
        sessions_revoked=sessions_revoked,
    )
    logger.info(
        "user.deprovisioned user_id=%s reason=%s actor=%s sessions_revoked=%s api_keys_deactivated=%s",
        user_id,
        reason,
        actor_id,
        sessions_revoked,
        api_keys_deactivated,
    )


def record_user_reactivated(*, user_id: int, actor_id: int | None, inactive_api_keys: int) -> None:
    """Record one reactivation; the API keys are left as they are."""
    logger.info(
        "user.reactivated user_id=%s actor=%s inactive_api_keys=%s",
        user_id,
        actor_id,
        inactive_api_keys,
    )


class UserService:
    """Stateless; all state lives in the DB session (and Redis, for revoked sessions)."""

    async def deactivate(
        self,
        db: AsyncSession,
        user: User,
        *,
        reason: str,
        actor_id: int | None,
        provider_slug: str | None = None,
        issuer: str | None = None,
        subject: str | None = None,
    ) -> DeactivationResult:
        """Deactivate ``user``, revoke its sessions and switch its API keys off.

        ``provider_slug`` / ``issuer`` / ``subject`` name the identity whose
        refused sign-in caused it, for the audit trail.

        Raises :class:`LastActiveAdminError` when ``user`` is the last active
        administrator. Deactivating an account that already is deactivated
        sweeps again (anything left over is revoked) and logs again.
        """
        refuse_system_user(user)
        if user.is_admin and user.status == "active" and not await another_active_admin_exists(db, user.id):
            raise LastActiveAdminError()

        user.status = "deactivated"
        user.deactivation_reason = reason[:64]
        await db.flush()
        sessions = await SessionService().revoke_user_sessions(
            db, user.id, reason=DEACTIVATION_REVOKE_REASON, actor_id=actor_id
        )
        keys = await ApiKeyService().deactivate_all(db, user.id)
        record_user_deprovisioned(
            user_id=user.id,
            reason=reason,
            actor_id=actor_id,
            sessions_revoked=sessions,
            api_keys_deactivated=keys,
            provider_slug=provider_slug,
            issuer=issuer,
            subject=subject,
        )
        return DeactivationResult(sessions_revoked=sessions, api_keys_deactivated=keys)

    async def reactivate(self, db: AsyncSession, user: User, *, actor_id: int | None) -> int:
        """Set a deactivated ``user`` active again; returns how many of its API keys stay inactive.

        Only a deactivated account changes; any other status (locked, waiting
        for email verification) is left alone, so this never skips a check.
        """
        refuse_system_user(user)
        changed = user.status == "deactivated"
        if changed:
            user.status = "active"
            user.deactivation_reason = None
            user.locked_until = None
            user.failed_login_count = 0
            await db.flush()
        inactive = int(
            await db.scalar(
                select(func.count()).select_from(ApiKey).where(ApiKey.user_id == user.id, ApiKey.is_active.is_(False))
            )
            or 0
        )
        if changed:
            record_user_reactivated(user_id=user.id, actor_id=actor_id, inactive_api_keys=inactive)
        return inactive
