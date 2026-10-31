"""SCIM ``/Users``: Specivo accounts managed by one SSO provider's SCIM client.

Every lookup is scoped to the provider the token belongs to: a user
provisioned by provider A answers 404 through provider B's URL and token.

**Create** (POST). A live ``userName`` already used at this provider is 409
``uniqueness``. Otherwise an account with the same email (case-insensitive)
is *linked* instead of a new one being created, but only if it is not an
administrator, not a service account, not a system account (login starting
with ``$``) and not managed by another provider's SCIM; anything else is 409.
When the ``userName`` is itself a valid login, an account with that login is
linked only if its email is the same too: an account with that login and
another email is 409 (the same login says nothing about the same person).
An adopted account whose email was never verified (or that waits for
verification) proved nothing about who holds it: its password, sessions, API
keys, password-reset tokens and SSO identities are removed before it is
marked verified (action ``adopted_unverified``), so whoever set it up with
the person's address keeps no way in. An
account this provider deleted through SCIM earlier is revived under a new id.
A new account gets the normalised ``userName`` as login (``-2``, ``-3`` ...
when only an email-shaped ``userName``'s local part collides), no password,
and its email marked verified: the IdP vouches for it.

**Update** (PUT, PATCH). PATCH is applied to the current representation and
then handled as a PUT (:mod:`specivo.services.scim.patch`). The login follows
a changed ``userName`` when the new login is free. ``active`` false
deactivates through :meth:`specivo.services.user_service.UserService.deactivate`
(sessions revoked, API keys switched off, ``USER_DEPROVISIONED`` with reason
``scim_deprovision``); true reactivates (the API keys stay off), but only an
account SCIM deactivated: one deactivated in Specivo (by an administrator, by
an SSO policy) stays off until an administrator reactivates it.

**Protected accounts.** An account that became an administrator or a service
account after it was provisioned (or a system account) is never deactivated,
deleted or given another email or login through SCIM: 400 with a ``detail``,
and the refusal is audited (``SCIM_REQUEST_REFUSED``). Its other attributes
still follow the IdP.

**Delete** deactivates the account the same way, removes the group
memberships this provider manages for it, and marks the resource deleted (it
answers 404 from then on). The account itself stays, so issues and history
keep their author.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.utils import utcnow
from specivo.models.auth import PasswordResetToken
from specivo.models.scim import ScimResource
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.models.user_group import UserGroupMember
from specivo.services.api_key_service import ApiKeyService
from specivo.services.permission_service import clear_role_cache
from specivo.services.scim import events
from specivo.services.scim.config_service import scim_base_url
from specivo.services.scim.errors import ScimError
from specivo.services.scim.filters import Comparison
from specivo.services.scim.mapping import UserAttributes, user_attributes
from specivo.services.scim.patch import apply_patch
from specivo.services.scim.resources import USER_SCHEMA, meta
from specivo.services.security_audit_service import AuditEvent
from specivo.services.session_service import SessionService
from specivo.services.sso.provisioning import unique_login
from specivo.services.user_service import LastActiveAdminError, UserService

__all__ = ["SCIM_DEPROVISION_REASON", "ScimUserService"]

#: ``reason`` of the deactivations SCIM causes (``USER_DEPROVISIONED``, revoked sessions).
SCIM_DEPROVISION_REASON = "scim_deprovision"
#: ``reason`` of the sessions revoked when SCIM adopts an account with an unproven email.
SCIM_ADOPTED_UNVERIFIED_REASON = "scim_adopted_unverified"

_PROTECTED_DETAIL = {
    "administrator": (
        "This account is an administrator in Specivo. SCIM does not deactivate or delete administrators "
        "or change their email address; remove the administrator rights in Specivo first."
    ),
    "service_account": (
        "This account is a service account in Specivo. SCIM does not deactivate or delete it "
        "or change its email address."
    ),
    "system_account": "This is a Specivo system account; SCIM does not change it.",
}


def _check_email_domain(provider: SsoProvider, email: str) -> None:
    """Refuse an email outside the provider's ``allowed_email_domains``, when it lists any."""
    allowed = {
        d.strip().lstrip("@").rstrip(".").lower()
        for d in provider.allowed_email_domains or ()
        if isinstance(d, str) and d.strip()
    }
    if allowed and email.rsplit("@", 1)[-1].rstrip(".").lower() not in allowed:
        raise ScimError.invalid_value("The email address is not in a domain this identity provider may use.")


def _protection(user: User) -> str | None:
    """Why SCIM must not deactivate ``user`` or change its email, or ``None``."""
    if user.is_system_principal:
        return "system_account"
    if user.is_admin:
        return "administrator"
    if user.is_service_account:
        return "service_account"
    return None


class ScimUserService:
    """Stateless; every method takes the provider whose token authenticated the request."""

    # -----------------------------------------------------------------------
    # Reading
    # -----------------------------------------------------------------------

    @staticmethod
    def _live(provider: SsoProvider) -> list[ColumnElement[bool]]:
        return [
            ScimResource.provider_id == provider.id,
            ScimResource.resource_type == "User",
            ScimResource.deleted_at.is_(None),
        ]

    @staticmethod
    def _condition(comparison: Comparison) -> ColumnElement[bool]:
        value = comparison.value
        if comparison.attribute == "userName":
            return func.lower(ScimResource.user_name) == value.lower()
        if comparison.attribute == "externalId":
            return ScimResource.external_id == value
        if comparison.attribute == "id":
            return ScimResource.scim_id == value
        if comparison.attribute == "emails.value":
            return func.lower(User.email) == value.lower()
        raise ScimError.invalid_filter("Filtering on this attribute is not supported.")  # pragma: no cover

    async def search(
        self,
        db: AsyncSession,
        provider: SsoProvider,
        *,
        comparisons: Sequence[Comparison],
        start_index: int,
        count: int,
    ) -> tuple[int, list[tuple[ScimResource, User]]]:
        """The total and one page (``start_index`` is 1-based) of the provider's live users."""
        conditions = [*self._live(provider), *(self._condition(c) for c in comparisons)]
        base = select(ScimResource, User).join(User, User.id == ScimResource.user_id).where(*conditions)
        total = int(
            await db.scalar(
                select(func.count())
                .select_from(ScimResource)
                .join(User, User.id == ScimResource.user_id)
                .where(*conditions)
            )
            or 0
        )
        if count == 0:
            return total, []
        rows = await db.execute(base.order_by(ScimResource.id).offset(start_index - 1).limit(count))
        return total, [(res, user) for res, user in rows.all()]

    async def get(self, db: AsyncSession, provider: SsoProvider, scim_id: str) -> tuple[ScimResource, User]:
        row = (
            await db.execute(
                select(ScimResource, User)
                .join(User, User.id == ScimResource.user_id)
                .where(*self._live(provider), ScimResource.scim_id == scim_id)
            )
        ).first()
        if row is None:
            raise ScimError.not_found("User not found")
        return row[0], row[1]

    @staticmethod
    def to_resource(provider: SsoProvider, res: ScimResource, user: User) -> dict[str, Any]:
        """The SCIM User for ``user``."""
        location = f"{scim_base_url(provider.slug)}/Users/{res.scim_id}"
        body: dict[str, Any] = {
            "schemas": [USER_SCHEMA],
            "id": res.scim_id,
            "userName": res.user_name,
            "name": {"formatted": user.display_name},
            "displayName": user.display_name,
            "emails": [{"value": user.email, "type": "work", "primary": True}],
            "active": user.status != "deactivated",
            "preferredLanguage": user.language,
            "timezone": user.timezone,
        }
        if res.external_id is not None:
            body["externalId"] = res.external_id
        modified = max((t for t in (res.updated_at, user.updated_at) if t is not None), default=None)
        body["meta"] = meta("User", res.created_at, modified, location)
        return body

    # -----------------------------------------------------------------------
    # Create
    # -----------------------------------------------------------------------

    @staticmethod
    def _attributes(body: Any) -> UserAttributes:
        if not isinstance(body, dict):
            raise ScimError.invalid_syntax("The request body must be a JSON object.")
        return user_attributes(body, languages=get_settings().available_languages)

    async def _require_user_name_free(
        self, db: AsyncSession, provider: SsoProvider, user_name: str, *, except_id: int | None = None
    ) -> None:
        stmt = select(ScimResource.id).where(
            *self._live(provider), func.lower(ScimResource.user_name) == user_name.lower()
        )
        if except_id is not None:
            stmt = stmt.where(ScimResource.id != except_id)
        if (await db.execute(stmt.limit(1))).first() is not None:
            raise ScimError.uniqueness("A user with this userName already exists.")

    @staticmethod
    async def _user_with(db: AsyncSession, column: Any, value: str) -> User | None:
        return (await db.execute(select(User).where(func.lower(column) == value.lower()).limit(1))).scalar_one_or_none()

    async def _existing_account(self, db: AsyncSession, attrs: UserAttributes) -> User | None:
        """The account a new SCIM user would be linked to: the one with its email."""
        by_email = await self._user_with(db, User.email, attrs.email)
        # Only a userName that already is a login may match one: the local part
        # of an email-shaped userName says nothing about who owns that login.
        by_login = (
            await self._user_with(db, User.login, attrs.login) if attrs.user_name.lower() == attrs.login else None
        )
        if by_login is not None and (by_email is None or by_email.id != by_login.id):
            # Linking it would give its owner's account (and a password reset
            # to the new address) to whoever the IdP calls by that name.
            raise ScimError.uniqueness("An account with this login already exists with another email address.")
        return by_email

    @staticmethod
    async def _link_target(db: AsyncSession, provider: SsoProvider, user: User) -> ScimResource | None:
        """Check that ``user`` may be linked; returns its deleted resource at this provider, if any."""
        if user.is_admin or user.is_service_account or user.is_system_principal:
            raise ScimError.uniqueness(
                "An account with this userName or email already exists. Administrators, service accounts "
                "and system accounts are never linked through SCIM."
            )
        res = (await db.execute(select(ScimResource).where(ScimResource.user_id == user.id))).scalar_one_or_none()
        if res is not None and res.provider_id != provider.id:
            raise ScimError.uniqueness(
                "An account with this userName or email is managed by another identity provider."
            )
        if res is not None and res.deleted_at is None:
            raise ScimError.uniqueness("An account with this email is already provisioned under another userName.")
        return res

    @staticmethod
    async def _wipe_if_unproven(db: AsyncSession, provider: SsoProvider, user: User) -> bool:
        """Remove every credential of ``user`` if its email was never proven; ``True`` when it did.

        Such an account may have been set up by somebody else with the
        person's address: its password, sessions, API keys, password-reset
        tokens and SSO identities are theirs, not the person's.
        """
        if user.email_verified_at is not None and user.status != "pending_verification":
            return False
        user.password_hash = None
        user.password_changed_at = None
        user.must_change_password = False
        user.failed_login_count = 0
        user.locked_until = None
        await db.flush()
        sessions = await SessionService().revoke_user_sessions(
            db, user.id, reason=SCIM_ADOPTED_UNVERIFIED_REASON, actor_id=None
        )
        keys = await ApiKeyService().deactivate_all(db, user.id)
        resets = await db.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id).returning(PasswordResetToken.id)
        )
        identities = await db.execute(
            delete(UserIdentity).where(UserIdentity.user_id == user.id).returning(UserIdentity.id)
        )
        events.logger.warning(
            "scim.user.adopted_unverified provider=%s user_id=%s sessions_revoked=%s api_keys_deactivated=%s "
            "reset_tokens_removed=%s identities_removed=%s",
            provider.slug,
            user.id,
            sessions,
            keys,
            len(resets.all()),
            len(identities.all()),
        )
        return True

    async def _new_user(self, db: AsyncSession, attrs: UserAttributes) -> User:
        login = attrs.login
        if await self._user_with(db, User.login, login) is not None:
            login = await unique_login(db, login)
        settings = get_settings()
        user = User(
            login=login,
            email=attrs.email,
            password_hash=None,
            display_name=attrs.display_name,
            language=attrs.language or settings.default_language,
            timezone=attrs.timezone or "UTC",
            status="active" if attrs.active else "deactivated",
            # Created switched off by the IdP, so the IdP may switch it on.
            deactivation_reason=None if attrs.active else SCIM_DEPROVISION_REASON,
            is_admin=False,
            is_service_account=False,
            must_change_password=False,
            # The IdP vouches for the address.
            email_verified_at=utcnow(),
        )
        savepoint = await db.begin_nested()
        try:
            db.add(user)
            await db.flush()
        except IntegrityError:
            await savepoint.rollback()
            raise ScimError.uniqueness("An account with this userName or email already exists.") from None
        if savepoint.is_active:
            await savepoint.commit()
        return user

    async def create(self, db: AsyncSession, provider: SsoProvider, body: Any) -> tuple[ScimResource, User]:
        attrs = self._attributes(body)
        _check_email_domain(provider, attrs.email)
        await self._require_user_name_free(db, provider, attrs.user_name)
        user = await self._existing_account(db, attrs)
        scim_id = str(uuid.uuid4())

        if user is None:
            user = await self._new_user(db, attrs)
            res = ScimResource(provider_id=provider.id, resource_type="User", user_id=user.id, scim_id=scim_id)
            action, fields = "created", list[str]()
        else:
            proven = user.email_verified_at is not None and user.status != "pending_verification"
            if proven and provider.link_policy != "auto_verified_email":
                # Taking over an account somebody already uses is the admin's
                # choice, the same one that lets sign-in link it by email.
                raise ScimError.uniqueness(
                    "An account with this email already exists, and this identity provider does not link "
                    "existing accounts (its link policy is manual)."
                )
            revived = await self._link_target(db, provider, user)
            res = revived or ScimResource(provider_id=provider.id, resource_type="User", user_id=user.id)
            res.scim_id = scim_id
            res.user_name = attrs.user_name
            res.external_id = attrs.external_id
            res.deleted_at = None
            action = "revived" if revived is not None else "linked"
            if await self._wipe_if_unproven(db, provider, user):
                action = "adopted_unverified"
            fields = await self._apply(db, provider, res, user, attrs, follow_login=False)

        res.user_name = attrs.user_name
        res.external_id = attrs.external_id
        savepoint = await db.begin_nested()
        try:
            db.add(res)
            await db.flush()
        except IntegrityError:
            await savepoint.rollback()
            raise ScimError.uniqueness("A user with this userName already exists.") from None
        if savepoint.is_active:
            await savepoint.commit()
        # Server-side timestamps (meta) are read back for the response.
        await db.refresh(res)
        await db.refresh(user)
        events.user_changed(
            AuditEvent.SCIM_USER_CREATED,
            provider_slug=provider.slug,
            scim_id=res.scim_id,
            user_id=user.id,
            action=action,
            fields=fields,
        )
        return res, user

    # -----------------------------------------------------------------------
    # Update
    # -----------------------------------------------------------------------

    @staticmethod
    def _refuse(provider: SsoProvider, res: ScimResource, user: User, reason: str) -> ScimError:
        events.request_refused(provider_slug=provider.slug, scim_id=res.scim_id, user_id=user.id, reason=reason)
        return ScimError(400, _PROTECTED_DETAIL[reason])

    async def _deactivate(self, db: AsyncSession, provider: SsoProvider, res: ScimResource, user: User) -> None:
        protection = _protection(user)
        if protection is not None:
            raise self._refuse(provider, res, user, protection)
        try:
            await UserService().deactivate(
                db,
                user,
                reason=SCIM_DEPROVISION_REASON,
                actor_id=None,
                provider_slug=provider.slug,
                issuer=provider.issuer,
            )
        except LastActiveAdminError:  # pragma: no cover - administrators are refused above
            raise self._refuse(provider, res, user, "administrator") from None

    async def _apply(
        self,
        db: AsyncSession,
        provider: SsoProvider,
        res: ScimResource,
        user: User,
        attrs: UserAttributes,
        *,
        follow_login: bool = True,
    ) -> list[str]:
        """Write ``attrs`` to ``res`` and ``user``; returns the SCIM attribute names that changed."""
        changed: list[str] = []
        protection = _protection(user)
        if protection is not None and attrs.email.lower() != user.email.lower():
            raise self._refuse(provider, res, user, protection)
        if res.id is not None and attrs.user_name != res.user_name:
            if attrs.user_name.lower() != (res.user_name or "").lower():
                await self._require_user_name_free(db, provider, attrs.user_name, except_id=res.id)
                if follow_login and protection is None and attrs.login != user.login.lower():
                    owner = await self._user_with(db, User.login, attrs.login)
                    if owner is None:
                        user.login = attrs.login
            res.user_name = attrs.user_name
            changed.append("userName")
        if res.id is not None and attrs.external_id != res.external_id:
            res.external_id = attrs.external_id
            changed.append("externalId")

        if attrs.email.lower() != user.email.lower():
            _check_email_domain(provider, attrs.email)
            if user.password_hash is not None:
                # A password reset would go to the address the IdP chose.
                raise ScimError.mutability(
                    "The email of an account with a Specivo password cannot be changed through SCIM."
                )
            owner = await self._user_with(db, User.email, attrs.email)
            if owner is not None and owner.id != user.id:
                raise ScimError.uniqueness("Another account has this email address.")
            user.email = attrs.email
            user.email_verified_at = utcnow()
            changed.append("emails")
        elif attrs.email != user.email:
            user.email = attrs.email
            changed.append("emails")
        if user.email_verified_at is None:
            user.email_verified_at = utcnow()

        if attrs.display_name != user.display_name:
            user.display_name = attrs.display_name
            changed.append("displayName")
        if attrs.language is not None and attrs.language != user.language:
            user.language = attrs.language
            changed.append("preferredLanguage")
        if attrs.timezone is not None and attrs.timezone != user.timezone:
            user.timezone = attrs.timezone
            changed.append("timezone")
        await db.flush()

        if attrs.active and user.status == "deactivated":
            if user.deactivation_reason == SCIM_DEPROVISION_REASON:
                await UserService().reactivate(db, user, actor_id=None)
                changed.append("active")
            else:
                # Switched off in Specivo (an administrator, an SSO policy):
                # only an administrator switches it on again.
                events.logger.info(
                    "scim.user.reactivation_skipped provider=%s user_id=%s scim_id=%s reason=%s",
                    provider.slug,
                    user.id,
                    res.scim_id,
                    user.deactivation_reason or "-",
                )
        elif attrs.active and user.status == "pending_verification":
            # Waiting only for its email to be verified, which the IdP now
            # vouches for; its credentials were removed first (_wipe_if_unproven).
            user.status = "active"
            await db.flush()
            changed.append("active")
        elif not attrs.active and user.status != "deactivated":
            await self._deactivate(db, provider, res, user)
            changed.append("active")
        return changed

    async def _update(
        self, db: AsyncSession, provider: SsoProvider, res: ScimResource, user: User, attrs: UserAttributes
    ) -> tuple[ScimResource, User]:
        was_active = user.status != "deactivated"
        wiped = await self._wipe_if_unproven(db, provider, user)
        fields = await self._apply(db, provider, res, user, attrs)
        if not fields and not wiped:
            return res, user
        is_active = user.status != "deactivated"
        if was_active and not is_active:
            event, action = AuditEvent.SCIM_USER_DEPROVISIONED, "deactivated"
        elif is_active and not was_active:
            event, action = AuditEvent.SCIM_USER_UPDATED, "reactivated"
        elif wiped:
            event, action = AuditEvent.SCIM_USER_UPDATED, "adopted_unverified"
        else:
            event, action = AuditEvent.SCIM_USER_UPDATED, "updated"
        await db.flush()
        await db.refresh(res)
        await db.refresh(user)
        events.user_changed(
            event, provider_slug=provider.slug, scim_id=res.scim_id, user_id=user.id, action=action, fields=fields
        )
        return res, user

    async def replace(
        self, db: AsyncSession, provider: SsoProvider, scim_id: str, body: Any
    ) -> tuple[ScimResource, User]:
        res, user = await self.get(db, provider, scim_id)
        return await self._update(db, provider, res, user, self._attributes(body))

    async def patch(
        self, db: AsyncSession, provider: SsoProvider, scim_id: str, request: Any
    ) -> tuple[ScimResource, User]:
        res, user = await self.get(db, provider, scim_id)
        current = self.to_resource(provider, res, user)
        patched = apply_patch(current, request)
        # The current representation holds the display name twice; whichever
        # one the request changed wins.
        old_formatted = current["name"]["formatted"]
        name = patched.get("name")
        new_formatted = name.get("formatted") if isinstance(name, dict) else None
        if patched.get("displayName") == current["displayName"] and new_formatted not in (None, old_formatted):
            patched["displayName"] = new_formatted
        return await self._update(db, provider, res, user, self._attributes(patched))

    # -----------------------------------------------------------------------
    # Delete
    # -----------------------------------------------------------------------

    async def delete(self, db: AsyncSession, provider: SsoProvider, scim_id: str) -> None:
        res, user = await self.get(db, provider, scim_id)
        protection = _protection(user)
        if protection is not None:
            raise self._refuse(provider, res, user, protection)
        if user.status != "deactivated":
            await self._deactivate(db, provider, res, user)
        removed = await db.execute(
            delete(UserGroupMember)
            .where(UserGroupMember.user_id == user.id, UserGroupMember.managed_by_provider_id == provider.id)
            .returning(UserGroupMember.group_id)
        )
        memberships = len(removed.all())
        if memberships:
            clear_role_cache(db)
        res.deleted_at = utcnow()
        await db.flush()
        events.user_changed(
            AuditEvent.SCIM_USER_DEPROVISIONED,
            provider_slug=provider.slug,
            scim_id=res.scim_id,
            user_id=user.id,
            action="deleted",
            memberships_removed=memberships,
        )
