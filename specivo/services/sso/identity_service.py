"""A user's connected SSO identities: listing them and disconnecting one.

Linking happens in the callback (:mod:`specivo.services.sso.provisioning`);
this module serves the profile's "Connected accounts" section, the admin
user page and the admin API.

Unlink rules (the "Flows -> Link an existing account" section of the SSO
design):

* a user may disconnect an identity only while another way to sign in
  remains: a password, or another identity at an *enabled* provider
  (``sso_last_sign_in_method``, 409, otherwise);
* an admin may always disconnect one;
* either way, every session signed in through that identity is revoked
  (:meth:`SessionService.revoke_by_identity`) before the row is deleted.

The user row is locked for the check, so two concurrent unlinks cannot both
see the other identity and leave the account without any way in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, NotFoundError
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.services.session_service import SessionService
from specivo.services.sso import events
from specivo.services.sso.events import UnlinkedBy

__all__ = ["ConnectedIdentity", "IdentityService", "LastSignInMethodError"]


class LastSignInMethodError(AppError):
    """The identity is the user's only way to sign in."""

    def __init__(self) -> None:
        super().__init__(
            code="sso_last_sign_in_method",
            message="This is the only way to sign in to the account",
            status_code=409,
        )


@dataclass(frozen=True)
class ConnectedIdentity:
    """An identity with its provider, as the profile and admin pages show it (no subject)."""

    id: int
    provider_id: int
    provider_slug: str
    provider_name: str
    provider_enabled: bool
    linked_via: str
    email_at_last_login: str | None
    last_login_at: datetime | None
    linked_at: datetime | None


@dataclass(frozen=True)
class UnlinkResult:
    """What an unlink removed."""

    identity_id: int
    provider_slug: str
    provider_name: str
    sessions_revoked: int


class IdentityService:
    """Stateless; receives the DB session on every call."""

    async def list_for_user(self, db: AsyncSession, user_id: int) -> list[ConnectedIdentity]:
        """The user's identities, ordered by provider position and name."""
        rows = (
            await db.execute(
                select(UserIdentity, SsoProvider)
                .join(SsoProvider, SsoProvider.id == UserIdentity.provider_id)
                .where(UserIdentity.user_id == user_id)
                .order_by(SsoProvider.position, SsoProvider.display_name, UserIdentity.id)
            )
        ).all()
        return [
            ConnectedIdentity(
                id=identity.id,
                provider_id=provider.id,
                provider_slug=provider.slug,
                provider_name=provider.display_name,
                provider_enabled=provider.enabled,
                linked_via=identity.linked_via,
                email_at_last_login=identity.email_at_last_login,
                last_login_at=identity.last_login_at,
                linked_at=identity.created_at,
            )
            for identity, provider in rows
        ]

    async def linkable_providers(self, db: AsyncSession, user_id: int) -> list[SsoProvider]:
        """Enabled providers where the user has no identity yet (the "Connect" buttons)."""
        linked = select(UserIdentity.provider_id).where(UserIdentity.user_id == user_id)
        return list(
            (
                await db.execute(
                    select(SsoProvider)
                    .where(SsoProvider.enabled.is_(True), SsoProvider.id.not_in(linked))
                    .order_by(SsoProvider.position, SsoProvider.display_name)
                )
            )
            .scalars()
            .all()
        )

    async def unlink(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        identity_id: int,
        by: UnlinkedBy,
        actor_id: int,
    ) -> UnlinkResult:
        """Disconnect identity ``identity_id`` from user ``user_id``.

        ``NotFoundError`` when the identity is not that user's. With
        ``by="user"`` the user must keep another way to sign in
        (:class:`LastSignInMethodError`); ``by="admin"`` always may.
        """
        user = (
            await db.execute(
                select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        row = (
            await db.execute(
                select(UserIdentity, SsoProvider)
                .join(SsoProvider, SsoProvider.id == UserIdentity.provider_id)
                .where(UserIdentity.id == identity_id, UserIdentity.user_id == user_id)
            )
        ).first()
        if user is None or row is None:
            raise NotFoundError("Connected account not found")
        identity, provider = row

        if by == "user" and not user.password_hash:
            others = await db.scalar(
                select(func.count())
                .select_from(UserIdentity)
                .join(SsoProvider, SsoProvider.id == UserIdentity.provider_id)
                .where(
                    UserIdentity.user_id == user_id,
                    UserIdentity.id != identity.id,
                    SsoProvider.enabled.is_(True),
                )
            )
            if not others:
                raise LastSignInMethodError()

        revoked = await SessionService().revoke_by_identity(db, identity.id, reason=f"sso_unlink_by_{by}")
        subject = identity.subject
        await db.delete(identity)
        await db.flush()
        events.account_unlinked(
            provider_slug=provider.slug,
            user_id=user_id,
            subject=subject,
            by=by,
            actor_id=actor_id,
            sessions_revoked=revoked,
        )
        return UnlinkResult(
            identity_id=identity_id,
            provider_slug=provider.slug,
            provider_name=provider.display_name,
            sessions_revoked=revoked,
        )
