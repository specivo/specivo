"""Session revocation: the one primitive every "sign this out" goes through.

A session is one login. Its refresh-token rows share a ``session_id`` that
survives rotation, and its access tokens carry that id as the ``sid`` claim.
:meth:`SessionService.revoke` ends sessions completely:

1. deletes every refresh-token row of the sessions, so none can mint new
   access tokens;
2. sets ``revoked_session:{session_id}`` in Redis for one access-token
   lifetime, so ``_authenticate_jwt`` refuses the tokens already issued on the
   very next request instead of when they expire.

Logout, logout-all, single-session revocation and the password-change sweep
use it today; SSO back-channel logout, unlink and deprovisioning use the
identity helpers below.

Every revocation that ends at least one session is logged on
``specivo.auth.sessions`` and recorded as the ``SESSIONS_REVOKED`` security
audit event (persisted when the ``security_audit_log`` feature is
registered): the reason, the counts and the affected user ids. Its actor is
``actor_id`` when the caller names one (the user signing out, the admin),
``None`` for what the system or the IdP starts.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from typing import cast

from sqlalchemy import ColumnElement, CursorResult, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.auth import RefreshToken
from specivo.services.security_audit_service import AuditEvent, record_event

logger = logging.getLogger("specivo.auth.sessions")

# First key of the two-key advisory locks taken per session (the second is
# hashtext(session_id)). Two-key locks never collide with the single-bigint
# advisory locks used elsewhere.
SESSION_LOCK_NAMESPACE = 0x5E55


class SessionService:
    """Stateless; all state lives in the DB session and Redis."""

    async def revoke(
        self,
        db: AsyncSession,
        session_ids: Iterable[uuid.UUID],
        *,
        reason: str,
        actor_id: int | None = None,
    ) -> int:
        """Revoke *session_ids*: delete their refresh rows and mark them in Redis.

        Unknown ids are still marked, so an access token whose refresh row is
        already gone (expired, rotated away) is cut off as well. Returns the
        number of refresh-token rows deleted, for auditing.
        """
        ids = list(dict.fromkeys(session_ids))
        if not ids:
            return 0
        return await self._revoke_where(
            db, RefreshToken.session_id.in_(ids), reason=reason, extra_ids=ids, actor_id=actor_id
        )

    async def revoke_user_sessions(
        self,
        db: AsyncSession,
        user_id: int,
        *,
        reason: str,
        keep_session_id: uuid.UUID | None = None,
        spare_access_of: uuid.UUID | None = None,
        actor_id: int | None = None,
    ) -> int:
        """Revoke every session of *user_id*, except *keep_session_id* if given.

        *spare_access_of* names a session whose refresh rows are deleted like
        the rest but whose access tokens already issued are left to expire on
        their own. A password change by a Bearer client uses it: the client
        loses its refresh token, as every session does, but the request it
        just made with its access token is not cut off mid-flow.
        """
        where: ColumnElement[bool] = RefreshToken.user_id == user_id
        if keep_session_id is not None:
            where = where & (RefreshToken.session_id != keep_session_id)
        return await self._revoke_where(db, where, reason=reason, spare_id=spare_access_of, actor_id=actor_id)

    async def revoke_by_idp_sid(
        self,
        db: AsyncSession,
        *,
        user_identity_id: int,
        idp_sid: str,
        reason: str,
        actor_id: int | None = None,
    ) -> int:
        """Revoke the sessions one IdP session created (back-channel logout with ``sid``)."""
        where = (RefreshToken.user_identity_id == user_identity_id) & (RefreshToken.idp_sid == idp_sid)
        return await self._revoke_where(db, where, reason=reason, actor_id=actor_id)

    async def revoke_by_identity(
        self,
        db: AsyncSession,
        user_identity_id: int,
        *,
        reason: str,
        actor_id: int | None = None,
    ) -> int:
        """Revoke every session signed in through one external identity (unlink, logout by ``sub``)."""
        return await self._revoke_where(
            db, RefreshToken.user_identity_id == user_identity_id, reason=reason, actor_id=actor_id
        )

    async def session_for_refresh_token(self, db: AsyncSession, token_hash: str) -> tuple[uuid.UUID, int] | None:
        """Return *(session_id, user_id)* of a refresh token (by its SHA-256 hash), or ``None``."""
        row = (
            await db.execute(
                select(RefreshToken.session_id, RefreshToken.user_id).where(RefreshToken.token_hash == token_hash)
            )
        ).first()
        return (row[0], row[1]) if row is not None else None

    async def lock_sessions(self, db: AsyncSession, session_ids: Iterable[uuid.UUID]) -> None:
        """Take a transaction-scoped advisory lock on each session, in a fixed order.

        Refresh and revocation both lock the session before touching its rows,
        so they serialise: a revocation that waits for a refresh then deletes
        the row the refresh inserted (its DELETE starts after the refresh
        committed), and a refresh that waits for a revocation finds its old row
        gone and is refused. Without this, a revocation's DELETE could miss a
        row inserted by a refresh that committed while it was blocked.
        """
        for sid in sorted({str(s) for s in session_ids}):
            await db.execute(
                text("SELECT pg_advisory_xact_lock(:ns, hashtext(:sid))"),
                {"ns": SESSION_LOCK_NAMESPACE, "sid": sid},
            )

    # ------------------------------------------------------------------

    async def _revoke_where(
        self,
        db: AsyncSession,
        where: ColumnElement[bool],
        *,
        reason: str,
        extra_ids: list[uuid.UUID] | None = None,
        spare_id: uuid.UUID | None = None,
        actor_id: int | None = None,
    ) -> int:
        from specivo.core.security import mark_sessions_revoked

        # Lock every session about to be revoked before deleting, so a refresh
        # in flight either finishes first (and its new row is deleted here) or
        # waits and is refused (see lock_sessions).
        found = (await db.execute(select(RefreshToken.session_id).where(where).distinct())).scalars().all()
        await self.lock_sessions(db, [*found, *(extra_ids or [])])

        stmt = delete(RefreshToken).where(where).returning(RefreshToken.session_id, RefreshToken.user_id)
        # execute() is typed as returning Result; a DML statement yields a
        # CursorResult at runtime.
        result = cast(CursorResult, await db.execute(stmt))
        rows = result.all()
        await db.flush()

        session_ids = list(dict.fromkeys([*(extra_ids or []), *(row[0] for row in rows)]))
        await mark_sessions_revoked([sid for sid in session_ids if sid != spare_id])

        details = {
            "reason": reason,
            "user_ids": sorted({row[1] for row in rows}),
            "session_count": len(session_ids),
            "row_count": len(rows),
        }
        logger.info("sessions revoked", extra={"event": AuditEvent.SESSIONS_REVOKED.value, **details})
        if session_ids:
            try:
                record_event(AuditEvent.SESSIONS_REVOKED, user_id=actor_id, details=details)
            except Exception:
                logger.warning("Security audit logging failed for a session revocation", exc_info=True)
        return len(rows)
