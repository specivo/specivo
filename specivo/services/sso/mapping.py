"""Access policy and group mapping of an SSO login (the "Claims and group mapping" design).

Two steps, both driven by :func:`specivo.services.sso.provisioning.resolve_user`
on a login (never on a link):

:func:`enforce_access` runs **before** an account is created, linked or
signed in, so a refused user is never provisioned or linked:

1. ``allowed_email_domains`` (when set): the domain must be one of them,
   exactly and case-insensitively (a subdomain is not a match), else
   ``sso_domain_not_allowed``. The domain is Google's ``hd`` claim when the
   issuer is exactly ``https://accounts.google.com`` (Google sets it from
   Workspace membership; any other IdP's ``hd`` is ignored). Otherwise it is
   the email's domain, and only a trusted, verified email counts
   (``trust_email_verified`` on and ``email_verified`` the boolean ``true``):
   on IdPs where users edit their own email, an unverified address in an
   allowed domain proves nothing.
2. ``required_groups`` (when set): the user must be in at least one. A
   missing groups claim counts as no groups (``sso_not_in_required_group``);
   an Entra group overage makes the groups unknown (``sso_groups_overage``).
3. A group overage is also refused when the admin flag is synced from
   ``admin_groups``: the groups decide who is an admin, and they are unknown.

A refusal revokes the sessions the identity started (it lost access at the
IdP; the "Deprovisioning (c)" rule), committed before the refusal is raised
so that a later failure in the request cannot undo it, and is logged with
``sso.login.refused_policy``. With the provider's
``deactivate_on_access_loss`` on, the account already linked to that identity
is also deactivated (:meth:`specivo.services.user_service.UserService.deactivate`:
every session, every API key), except a service account (which cannot hold an
identity anyway) and the last active admin, which is kept with a warning
(``sso.deprovision.skipped``). An account that would only be auto-linked by
email is never deactivated: it has not signed in through this provider. Every overage is logged with
``sso.groups.overage`` and tells the admin what to configure.

:func:`apply_mapping` runs once the user is known, only with a :class:`Known`
groups claim (a missing claim or an overage changes nothing, so a broken IdP
mapper never removes access en masse):

4. ``sync_admin_flag``: ``is_admin = bool(groups & admin_groups)``. The
   **last-admin guard** skips a demotion that would leave no active admin who
   can sign in, with a warning.
5. ``sync_groups`` (skipped while the provider's SCIM is on: SCIM then owns
   the memberships): each ``SsoGroupMapping`` whose IdP group (exact,
   case-sensitive) is in the claim adds a membership marked
   ``managed_by_provider_id``; memberships this provider manages whose group
   is no longer mapped from the claim are removed. Memberships added by hand
   or managed by another provider are never touched, and no second row is
   created next to them. The role cache is cleared after a change, so the
   project roles held by the groups apply at once.

Only users with an identity at the provider ever reach :func:`apply_mapping`.
Mapping the same claims twice writes nothing and logs nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.sso import SsoGroupMapping, SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.models.user_group import UserGroupMember
from specivo.services.permission_service import clear_role_cache
from specivo.services.session_service import SessionService
from specivo.services.sso import events
from specivo.services.sso.claims import GroupsResult, Known, Missing, Overage, trusted_verified_email
from specivo.services.sso.errors import (
    SsoAccessPolicyError,
    SsoDomainNotAllowedError,
    SsoGroupsOverageError,
    SsoNotInRequiredGroupError,
)
from specivo.services.sso.log import logger
from specivo.services.user_service import LastActiveAdminError, UserService, another_active_admin_exists

__all__ = ["GOOGLE_ISSUER", "POLICY_REVOKE_REASON", "apply_mapping", "check_access", "enforce_access"]

#: ``reason`` recorded on the sessions revoked by a policy refusal.
POLICY_REVOKE_REASON = "sso_access_lost"

#: The only issuer whose ``hd`` (hosted domain) claim is honoured.
GOOGLE_ISSUER = "https://accounts.google.com"


# ---------------------------------------------------------------------------
# Access policy
# ---------------------------------------------------------------------------


def _normalise_domain(value: str) -> str:
    return value.strip().lstrip("@").rstrip(".").lower()


def _domain_refusal(
    provider: SsoProvider, claims: Mapping[str, Any], email: str | None, allowed: set[str]
) -> SsoDomainNotAllowedError | None:
    """Check the allowlist against Google's ``hd`` or a trusted, verified email domain."""
    hd = claims.get("hd")
    if provider.issuer == GOOGLE_ISSUER and isinstance(hd, str) and hd.strip():
        return None if _normalise_domain(hd) in allowed else SsoDomainNotAllowedError("hd_domain_not_allowed")
    if not email or "@" not in email:
        return SsoDomainNotAllowedError("no_email_domain")
    if not trusted_verified_email(provider, claims):
        return SsoDomainNotAllowedError("email_not_verified")
    if _normalise_domain(email.rsplit("@", 1)[1]) not in allowed:
        return SsoDomainNotAllowedError("email_domain_not_allowed")
    return None


def check_access(
    provider: SsoProvider, claims: Mapping[str, Any], groups: GroupsResult, email: str | None
) -> SsoAccessPolicyError | None:
    """The refusal ``provider``'s policy gives this login, or ``None`` when it may proceed."""
    allowed = {_normalise_domain(d) for d in provider.allowed_email_domains or () if isinstance(d, str) and d.strip()}
    if allowed:
        refusal = _domain_refusal(provider, claims, email, allowed)
        if refusal is not None:
            return refusal

    required = {g for g in provider.required_groups or () if isinstance(g, str)}
    if required:
        if isinstance(groups, Overage):
            return SsoGroupsOverageError("groups_overage_required_groups")
        if isinstance(groups, Missing):
            return SsoNotInRequiredGroupError("groups_claim_missing")
        if not groups.groups & required:
            return SsoNotInRequiredGroupError("not_in_required_group")

    if isinstance(groups, Overage) and provider.sync_admin_flag and provider.admin_groups:
        return SsoGroupsOverageError("groups_overage_admin_groups")
    return None


async def enforce_access(
    db: AsyncSession,
    *,
    provider: SsoProvider,
    claims: Mapping[str, Any],
    groups: GroupsResult,
    email: str | None,
    subject: str,
    user: User | None = None,
    identity: UserIdentity | None = None,
) -> None:
    """Raise the policy refusal for this login, after revoking ``identity``'s sessions.

    ``user`` and ``identity`` are the account the login would use, when one
    exists (an existing identity, or the target of an automatic link).
    """
    refusal = check_access(provider, claims, groups, email)
    user_id = user.id if user is not None else None
    if isinstance(groups, Overage):
        events.groups_overage(provider_slug=provider.slug, user_id=user_id, subject=subject, denied=refusal is not None)
    if refusal is None:
        return

    # The revocation marks the sessions revoked in Redis at once, so its
    # deleted rows are committed at once too: a step that fails later in the
    # request (deactivation, audit, the response) must not roll them back
    # while the Redis markers stay. The markers expire with the access token
    # lifetime; rows brought back by a rollback would then refresh again.
    revoked = 0
    if identity is not None:
        revoked = await SessionService().revoke_by_identity(db, identity.id, reason=POLICY_REVOKE_REASON)
        await db.commit()
    events.login_refused_policy(
        provider_slug=provider.slug,
        user_id=user_id,
        subject=subject,
        code=refusal.code,
        reason=refusal.reason,
        sessions_revoked=revoked,
    )
    if provider.deactivate_on_access_loss and user is not None and identity is not None:
        await _deactivate_on_access_loss(db, provider, user, subject)
        await db.commit()  # the same reason: it revoked every session of the account
    raise refusal


async def _deactivate_on_access_loss(db: AsyncSession, provider: SsoProvider, user: User, subject: str) -> None:
    """Deactivate ``user`` after a policy refusal; never a service account or the last active admin."""
    if user.is_service_account or user.status != "active":
        return
    try:
        await UserService().deactivate(
            db,
            user,
            reason=POLICY_REVOKE_REASON,
            actor_id=None,
            provider_slug=provider.slug,
            issuer=provider.issuer,
            subject=subject,
        )
    except LastActiveAdminError:
        logger.warning(
            "sso.deprovision.skipped provider=%s user_id=%s reason=last_active_admin "
            "(deactivation_skipped: the account keeps working; make another user an administrator)",
            provider.slug,
            user.id,
        )


# ---------------------------------------------------------------------------
# Admin flag and user groups
# ---------------------------------------------------------------------------


async def _sync_admin_flag(
    db: AsyncSession, provider: SsoProvider, user: User, groups: frozenset[str], subject: str
) -> None:
    wanted = bool(groups & {g for g in provider.admin_groups or () if isinstance(g, str)})
    if wanted == user.is_admin:
        return
    if wanted:
        user.is_admin = True
    elif await another_active_admin_exists(db, user.id):
        user.is_admin = False
    else:
        events.admin_demotion_skipped(provider_slug=provider.slug, user_id=user.id, subject=subject)


async def _sync_groups(
    db: AsyncSession, provider: SsoProvider, user: User, groups: frozenset[str]
) -> tuple[list[int], list[int]]:
    """Add and remove this provider's managed memberships; returns the user group ids changed."""
    mappings = (
        await db.execute(
            select(SsoGroupMapping.idp_group, SsoGroupMapping.user_group_id).where(
                SsoGroupMapping.provider_id == provider.id
            )
        )
    ).all()
    wanted = {group_id for idp_group, group_id in mappings if idp_group in groups}
    rows = list((await db.execute(select(UserGroupMember).where(UserGroupMember.user_id == user.id))).scalars().all())
    present = {row.group_id for row in rows}

    added: list[int] = []
    for group_id in sorted(wanted - present):
        # A concurrent login may have added it: the unique (group, user) row wins.
        inserted = await db.execute(
            insert(UserGroupMember)
            .values(group_id=group_id, user_id=user.id, managed_by_provider_id=provider.id)
            .on_conflict_do_nothing(constraint="uq_user_group_members_group_user")
            .returning(UserGroupMember.id)
        )
        if inserted.first() is not None:
            added.append(group_id)

    removed: list[int] = []
    for row in rows:
        if row.managed_by_provider_id == provider.id and row.group_id not in wanted:
            await db.delete(row)
            removed.append(row.group_id)
    return added, sorted(removed)


async def apply_mapping(
    db: AsyncSession,
    *,
    provider: SsoProvider,
    user: User,
    groups: GroupsResult,
    subject: str,
) -> bool:
    """Sync the admin flag and the managed memberships of ``user``; ``True`` when something changed.

    ``user`` must have an identity at ``provider`` (the callers guarantee it).
    """
    # With SCIM on, the provider pushes group memberships itself; the token's
    # groups claim must not rewrite them at every sign-in.
    sync_groups = provider.sync_groups and not provider.scim_enabled
    if not isinstance(groups, Known) or not (provider.sync_admin_flag or sync_groups):
        return False

    admin_before = user.is_admin
    if provider.sync_admin_flag:
        await _sync_admin_flag(db, provider, user, groups.groups, subject)
    added, removed = await _sync_groups(db, provider, user, groups.groups) if sync_groups else ([], [])

    if user.is_admin == admin_before and not added and not removed:
        return False
    await db.flush()
    clear_role_cache(db)
    events.roles_synced(
        provider_slug=provider.slug,
        user_id=user.id,
        subject=subject,
        admin_before=admin_before,
        admin_after=user.is_admin,
        groups_added=added,
        groups_removed=removed,
    )
    return True
