"""Service tests for SSO claims and group mapping (``specivo.services.sso.mapping``).

Covers the "Claims and group mapping" section of the SSO design, driven
through :func:`resolve_user` as the callback does:

- access policy (email domain allowlist with Google ``hd``, required groups,
  a missing groups claim, an Entra group overage) refuses before an account
  is created or linked, and a refusal ends the identity's existing sessions;
- the admin flag follows ``admin_groups``, with the last-admin guard;
- user-group sync adds and removes only memberships this provider manages,
  and the project roles held by the group follow;
- mapping is idempotent.

Logins, emails and group names carry a ``map`` prefix: modules run in parallel
inside uncommitted transactions and those columns are unique.
"""

from __future__ import annotations

import itertools
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.auth import RefreshToken
from specivo.models.member import Member, MemberRole
from specivo.models.role import Role
from specivo.models.sso import SsoGroupMapping, SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.services.auth_service import AuthService
from specivo.services.permission_service import check_permission
from specivo.services.sso.errors import (
    SsoDomainNotAllowedError,
    SsoGroupsOverageError,
    SsoNotInRequiredGroupError,
)
from specivo.services.sso.id_token import IdTokenClaims
from specivo.services.sso.provisioning import ResolvedAccount, resolve_user
from specivo.testing.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory
from specivo.testing.sso import build_sso_provider, link_identity
from tests.factories.project import ProjectFactory

pytestmark = pytest.mark.service

SUB = "map-subject-1"
VERIFIED_AT = datetime(2026, 1, 1, tzinfo=UTC)
_counter = itertools.count(1)
OVERAGE = {"_claim_names": {"groups": "src1"}, "_claim_sources": {"src1": {"endpoint": "https://graph.example.com/x"}}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _name(stem: str) -> str:
    return f"map-{stem}-{next(_counter)}"


def _token(provider: SsoProvider, sub: str = SUB) -> IdTokenClaims:
    return IdTokenClaims(
        issuer=provider.issuer, subject=sub, audience=(provider.client_id,), expires_at=0, issued_at=0, nonce="n"
    )


async def _resolve(
    db: AsyncSession, provider: SsoProvider, *, sub: str = SUB, intent: str = "login", **claims: Any
) -> ResolvedAccount:
    values: dict[str, Any] = {"email": "map-member@example.com", "email_verified": True, "name": "Mapped Member"}
    values.update(claims)
    return await resolve_user(
        db,
        provider=provider,
        id_token=_token(provider, sub),
        claims={k: v for k, v in values.items() if v is not None},
        intent=intent,  # type: ignore[arg-type]
    )


async def _user(db: AsyncSession, factory: Any = UserFactory, **fields: Any) -> User:
    fields.setdefault("email_verified_at", VERIFIED_AT)
    user = factory.build(**fields)
    db.add(user)
    await db.flush()
    return user


async def _linked(db: AsyncSession, provider: SsoProvider, factory: Any = UserFactory, **fields: Any) -> User:
    fields.setdefault("login", _name("user"))
    fields.setdefault("email", f"{fields['login']}@example.com")
    user = await _user(db, factory, **fields)
    await link_identity(db, user, provider, subject=SUB)
    return user


async def _group(db: AsyncSession, stem: str = "group") -> UserGroup:
    group = UserGroup(name=_name(stem))
    db.add(group)
    await db.flush()
    return group


async def _map(db: AsyncSession, provider: SsoProvider, idp_group: str, group: UserGroup) -> None:
    db.add(SsoGroupMapping(provider_id=provider.id, idp_group=idp_group, user_group_id=group.id))
    await db.flush()


async def _memberships(db: AsyncSession, user_id: int) -> dict[int, int | None]:
    """``{group_id: managed_by_provider_id}`` of the user's group memberships."""
    rows = await db.execute(
        select(UserGroupMember.group_id, UserGroupMember.managed_by_provider_id).where(
            UserGroupMember.user_id == user_id
        )
    )
    return {group_id: managed for group_id, managed in rows.all()}


async def _sso_session(db: AsyncSession, user: User, provider: SsoProvider) -> None:
    identity = (
        await db.execute(
            select(UserIdentity).where(UserIdentity.user_id == user.id, UserIdentity.provider_id == provider.id)
        )
    ).scalar_one()
    await AuthService().create_sso_session(
        db, user=user, identity=identity, provider=provider, id_token="not-a-real-token", idp_sid=None
    )


async def _session_count(db: AsyncSession, user_id: int) -> int:
    return int(await db.scalar(select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user_id)))


async def _user_count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(User)))


async def _only_admin(db: AsyncSession, user: User) -> None:
    """Make ``user`` the only active admin who can sign in (rolled back with the test)."""
    await db.execute(
        update(User).where(User.is_admin.is_(True), User.id != user.id).values(status="locked", locked_until=None)
    )


# ---------------------------------------------------------------------------
# Domain allowlist
# ---------------------------------------------------------------------------


GOOGLE_ISSUER = "https://accounts.google.com"


def _allow(provider: SsoProvider, *domains: str, trusted: bool = True) -> None:
    provider.allowed_email_domains = list(domains)
    provider.trust_email_verified = trusted


async def test_allowed_email_domain_signs_in(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    _allow(sso_provider, "example.com")
    user = await _linked(db_session, sso_provider)

    account = await _resolve(db_session, sso_provider, email="map-a@EXAMPLE.com")

    assert account.user.id == user.id


async def test_email_domain_outside_the_allowlist_is_refused(
    db_session: AsyncSession, sso_provider: SsoProvider, caplog: pytest.LogCaptureFixture
) -> None:
    _allow(sso_provider, "corp.example.com")
    await _linked(db_session, sso_provider)

    with caplog.at_level(logging.INFO, logger="specivo.sso"), pytest.raises(SsoDomainNotAllowedError) as info:
        await _resolve(db_session, sso_provider, email="map-a@example.com")

    assert info.value.code == "sso_domain_not_allowed"
    assert "sso.login.refused_policy" in caplog.text
    assert "code=sso_domain_not_allowed" in caplog.text


@pytest.mark.parametrize(
    ("trusted", "email_verified"),
    [(False, True), (True, None), (True, False), (True, "true")],
    ids=["provider_not_trusted", "claim_missing", "claim_false", "claim_string_true"],
)
async def test_unverified_email_on_an_allowed_domain_is_refused(
    db_session: AsyncSession, sso_provider: SsoProvider, trusted: bool, email_verified: Any
) -> None:
    """An IdP that lets users set their own email must not open the allowlist."""
    _allow(sso_provider, "example.com", trusted=trusted)
    await _linked(db_session, sso_provider)

    with pytest.raises(SsoDomainNotAllowedError) as info:
        await _resolve(db_session, sso_provider, email="map-a@example.com", email_verified=email_verified)

    assert info.value.reason == "email_not_verified"


async def test_unverified_email_jit_user_is_not_created(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    _allow(sso_provider, "example.com", trusted=False)
    sso_provider.allow_jit_provisioning = True
    before = await _user_count(db_session)

    with pytest.raises(SsoDomainNotAllowedError):
        await _resolve(db_session, sso_provider, email="map-claimed@example.com")

    assert await _user_count(db_session) == before


async def test_subdomain_is_not_a_match(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    _allow(sso_provider, "example.com")
    await _linked(db_session, sso_provider)

    with pytest.raises(SsoDomainNotAllowedError):
        await _resolve(db_session, sso_provider, email="map-a@evil.example.com")


async def test_missing_email_is_refused_by_the_allowlist(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    _allow(sso_provider, "example.com")
    await _linked(db_session, sso_provider)

    with pytest.raises(SsoDomainNotAllowedError):
        await _resolve(db_session, sso_provider, email=None)


async def test_google_hd_decides_for_the_google_issuer(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.issuer = GOOGLE_ISSUER
    _allow(sso_provider, "example.com", trusted=False)
    user = await _linked(db_session, sso_provider)

    account = await _resolve(
        db_session, sso_provider, email="map-a@alias.example.org", email_verified=None, hd="example.com"
    )

    assert account.user.id == user.id


async def test_google_hd_mismatch_is_refused(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.issuer = GOOGLE_ISSUER
    _allow(sso_provider, "example.com")
    await _linked(db_session, sso_provider)

    with pytest.raises(SsoDomainNotAllowedError):
        await _resolve(db_session, sso_provider, email="map-a@example.com", hd="other.example.org")


async def test_google_without_hd_falls_back_to_the_verified_email(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    sso_provider.issuer = GOOGLE_ISSUER
    _allow(sso_provider, "example.com", trusted=False)
    await _linked(db_session, sso_provider)

    with pytest.raises(SsoDomainNotAllowedError) as info:
        await _resolve(db_session, sso_provider, email="map-a@example.com")

    assert info.value.reason == "email_not_verified"


async def test_hd_from_another_issuer_is_ignored(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    """Only Google sets ``hd`` from Workspace membership; elsewhere it is user-controllable."""
    _allow(sso_provider, "example.com", trusted=False)
    await _linked(db_session, sso_provider)

    with pytest.raises(SsoDomainNotAllowedError) as info:
        await _resolve(db_session, sso_provider, email="map-a@outside.example.org", hd="example.com")

    assert info.value.reason == "email_not_verified"


async def test_hd_from_another_issuer_falls_back_to_the_verified_email(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    _allow(sso_provider, "example.com")
    user = await _linked(db_session, sso_provider)

    account = await _resolve(db_session, sso_provider, email="map-a@example.com", hd="other.example.org")
    assert account.user.id == user.id

    with pytest.raises(SsoDomainNotAllowedError):
        await _resolve(db_session, sso_provider, email="map-a@outside.example.org", hd="example.com")


async def test_refused_jit_user_is_not_created(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.allow_jit_provisioning = True
    sso_provider.allowed_email_domains = ["corp.example.com"]
    before = await _user_count(db_session)

    with pytest.raises(SsoDomainNotAllowedError):
        await _resolve(db_session, sso_provider, email="map-jit@example.com")

    assert await _user_count(db_session) == before


async def test_refused_auto_link_is_not_linked(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.link_policy = "auto_verified_email"
    sso_provider.trust_email_verified = True
    sso_provider.required_groups = ["map-staff"]
    target = await _user(db_session, login=_name("auto"), email="map-auto@example.com")

    with pytest.raises(SsoNotInRequiredGroupError):
        await _resolve(db_session, sso_provider, email="map-auto@example.com", groups=["map-guests"])

    count = await db_session.scalar(
        select(func.count()).select_from(UserIdentity).where(UserIdentity.user_id == target.id)
    )
    assert count == 0


# ---------------------------------------------------------------------------
# Required groups
# ---------------------------------------------------------------------------


async def test_required_group_present_signs_in(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.required_groups = ["map-staff", "map-contractors"]
    user = await _linked(db_session, sso_provider)

    account = await _resolve(db_session, sso_provider, groups=["map-contractors"])

    assert account.user.id == user.id


async def test_required_group_absent_is_refused_and_sessions_revoked(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    sso_provider.required_groups = ["map-staff"]
    user = await _linked(db_session, sso_provider)
    await _sso_session(db_session, user, sso_provider)
    assert await _session_count(db_session, user.id) == 1

    with pytest.raises(SsoNotInRequiredGroupError) as info:
        await _resolve(db_session, sso_provider, groups=["map-guests"])

    assert info.value.code == "sso_not_in_required_group"
    assert await _session_count(db_session, user.id) == 0


async def test_refusal_keeps_password_sessions(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    """Only the sessions started through this identity end; a password session stays."""
    sso_provider.required_groups = ["map-staff"]
    user = await _linked(db_session, sso_provider)
    await _sso_session(db_session, user, sso_provider)
    await AuthService().login(db_session, user.login, TEST_PASSWORD)
    assert await _session_count(db_session, user.id) == 2

    with pytest.raises(SsoNotInRequiredGroupError):
        await _resolve(db_session, sso_provider, groups=[])

    [remaining] = (await db_session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))).scalars()
    assert remaining.user_identity_id is None


async def test_missing_claim_with_required_groups_is_refused(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    sso_provider.required_groups = ["map-staff"]
    await _linked(db_session, sso_provider)

    with pytest.raises(SsoNotInRequiredGroupError):
        await _resolve(db_session, sso_provider)


async def test_missing_claim_without_policy_signs_in(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    user = await _linked(db_session, sso_provider)
    account = await _resolve(db_session, sso_provider)
    assert account.user.id == user.id


async def test_required_groups_read_from_the_configured_path(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    sso_provider.claim_config = {"groups_claim": "resource_access.specivo.roles"}
    sso_provider.required_groups = ["map-user"]
    user = await _linked(db_session, sso_provider)

    account = await _resolve(
        db_session, sso_provider, groups=["map-other"], resource_access={"specivo": {"roles": ["map-user"]}}
    )

    assert account.user.id == user.id


async def test_link_intent_is_not_subject_to_the_policy(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.required_groups = ["map-staff"]
    user = await _user(db_session, login=_name("linker"), email="map-linker@example.com")

    account = await resolve_user(
        db_session,
        provider=sso_provider,
        id_token=_token(sso_provider),
        claims={"email": "map-linker@example.com"},
        intent="link",
        link_user_id=user.id,
    )

    assert account.outcome == "linked"


# ---------------------------------------------------------------------------
# Entra group overage
# ---------------------------------------------------------------------------


async def test_overage_with_required_groups_is_refused(
    db_session: AsyncSession, sso_provider: SsoProvider, caplog: pytest.LogCaptureFixture
) -> None:
    sso_provider.required_groups = ["map-staff"]
    await _linked(db_session, sso_provider)

    with caplog.at_level(logging.INFO, logger="specivo.sso"), pytest.raises(SsoGroupsOverageError) as info:
        await _resolve(db_session, sso_provider, **OVERAGE)

    assert info.value.code == "sso_groups_overage"
    assert "sso.groups.overage" in caplog.text
    assert "Groups assigned to the application" in caplog.text


async def test_overage_with_admin_sync_is_refused(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.sync_admin_flag = True
    sso_provider.admin_groups = ["map-admins"]
    await _linked(db_session, sso_provider)

    with pytest.raises(SsoGroupsOverageError):
        await _resolve(db_session, sso_provider, **OVERAGE)


async def test_overage_without_access_policy_signs_in_and_syncs_nothing(
    db_session: AsyncSession, sso_provider: SsoProvider, caplog: pytest.LogCaptureFixture
) -> None:
    sso_provider.sync_groups = True
    kept = await _group(db_session, "kept")
    await _map(db_session, sso_provider, "map-dev", kept)
    user = await _linked(db_session, sso_provider)
    db_session.add(UserGroupMember(group_id=kept.id, user_id=user.id, managed_by_provider_id=sso_provider.id))
    await db_session.flush()

    with caplog.at_level(logging.INFO, logger="specivo.sso"):
        account = await _resolve(db_session, sso_provider, **OVERAGE)

    assert account.user.id == user.id
    assert await _memberships(db_session, user.id) == {kept.id: sso_provider.id}
    assert "sso.groups.overage" in caplog.text


async def test_refused_overage_jit_user_is_not_created(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.allow_jit_provisioning = True
    sso_provider.required_groups = ["map-staff"]
    before = await _user_count(db_session)

    with pytest.raises(SsoGroupsOverageError):
        await _resolve(db_session, sso_provider, email="map-overjit@example.com", **OVERAGE)

    assert await _user_count(db_session) == before


# ---------------------------------------------------------------------------
# Admin flag
# ---------------------------------------------------------------------------


async def test_admin_group_promotes(
    db_session: AsyncSession, sso_provider: SsoProvider, caplog: pytest.LogCaptureFixture
) -> None:
    sso_provider.sync_admin_flag = True
    sso_provider.admin_groups = ["map-admins"]
    user = await _linked(db_session, sso_provider)

    with caplog.at_level(logging.INFO, logger="specivo.sso"):
        await _resolve(db_session, sso_provider, groups=["map-admins", "map-dev"])

    await db_session.refresh(user)
    assert user.is_admin is True
    assert "sso.roles.synced" in caplog.text
    assert "admin_before=False admin_after=True" in caplog.text


async def test_jit_user_in_admin_group_is_promoted(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.allow_jit_provisioning = True
    sso_provider.sync_admin_flag = True
    sso_provider.admin_groups = ["map-admins"]

    account = await _resolve(db_session, sso_provider, email="map-jitadmin@example.com", groups=["map-admins"])

    assert account.outcome == "provisioned"
    assert account.user.is_admin is True


async def test_admin_without_admin_group_is_demoted(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.sync_admin_flag = True
    sso_provider.admin_groups = ["map-admins"]
    await _user(db_session, AdminUserFactory, login=_name("otheradmin"), email="map-otheradmin@example.com")
    user = await _linked(db_session, sso_provider, AdminUserFactory)

    await _resolve(db_session, sso_provider, groups=["map-dev"])

    await db_session.refresh(user)
    assert user.is_admin is False


async def test_admin_flag_unchanged_when_claim_missing(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.sync_admin_flag = True
    sso_provider.admin_groups = ["map-admins"]
    await _user(db_session, AdminUserFactory, login=_name("otheradmin"), email="map-otheradmin2@example.com")
    user = await _linked(db_session, sso_provider, AdminUserFactory)

    await _resolve(db_session, sso_provider)

    await db_session.refresh(user)
    assert user.is_admin is True


async def test_admin_flag_not_synced_when_off(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.admin_groups = ["map-admins"]
    user = await _linked(db_session, sso_provider)

    await _resolve(db_session, sso_provider, groups=["map-admins"])

    await db_session.refresh(user)
    assert user.is_admin is False


async def test_last_admin_is_not_demoted(
    db_session: AsyncSession, sso_provider: SsoProvider, caplog: pytest.LogCaptureFixture
) -> None:
    sso_provider.sync_admin_flag = True
    sso_provider.admin_groups = ["map-admins"]
    user = await _linked(db_session, sso_provider, AdminUserFactory)
    await _only_admin(db_session, user)

    with caplog.at_level(logging.INFO, logger="specivo.sso"):
        account = await _resolve(db_session, sso_provider, groups=["map-dev"])

    assert account.user.id == user.id
    await db_session.refresh(user)
    assert user.is_admin is True
    assert "sso.roles.admin_demotion_skipped" in caplog.text
    assert any(r.levelno == logging.WARNING and "admin_demotion_skipped" in r.getMessage() for r in caplog.records)


async def test_service_account_admin_does_not_count_for_the_guard(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    sso_provider.sync_admin_flag = True
    sso_provider.admin_groups = ["map-admins"]
    user = await _linked(db_session, sso_provider, AdminUserFactory)
    await _only_admin(db_session, user)
    await _user(
        db_session,
        AdminUserFactory,
        login=_name("svcadmin"),
        email="map-svcadmin@example.com",
        is_service_account=True,
        password_hash=None,
    )

    await _resolve(db_session, sso_provider, groups=[])

    await db_session.refresh(user)
    assert user.is_admin is True


async def test_local_admin_without_identity_is_never_touched(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    sso_provider.sync_admin_flag = True
    sso_provider.admin_groups = ["map-admins"]
    local_admin = await _user(db_session, AdminUserFactory, login=_name("localadmin"), email="map-local@example.com")
    sso_admin = await _linked(db_session, sso_provider, AdminUserFactory)

    await _resolve(db_session, sso_provider, groups=[])

    await db_session.refresh(local_admin)
    await db_session.refresh(sso_admin)
    assert local_admin.is_admin is True
    assert sso_admin.is_admin is False


# ---------------------------------------------------------------------------
# User-group sync
# ---------------------------------------------------------------------------


async def test_sync_adds_managed_membership(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.sync_groups = True
    dev = await _group(db_session, "dev")
    ops = await _group(db_session, "ops")
    await _map(db_session, sso_provider, "map-dev", dev)
    await _map(db_session, sso_provider, "map-ops", ops)
    user = await _linked(db_session, sso_provider)

    await _resolve(db_session, sso_provider, groups=["map-dev", "map-unmapped"])

    assert await _memberships(db_session, user.id) == {dev.id: sso_provider.id}


async def test_group_names_are_matched_case_sensitively(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.sync_groups = True
    dev = await _group(db_session, "dev")
    await _map(db_session, sso_provider, "map-Dev", dev)
    user = await _linked(db_session, sso_provider)

    await _resolve(db_session, sso_provider, groups=["map-dev"])

    assert await _memberships(db_session, user.id) == {}


async def test_sync_removes_only_managed_memberships(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.sync_groups = True
    managed = await _group(db_session, "managed")
    manual = await _group(db_session, "manual")
    await _map(db_session, sso_provider, "map-managed", managed)
    await _map(db_session, sso_provider, "map-manual", manual)
    user = await _linked(db_session, sso_provider)
    db_session.add(UserGroupMember(group_id=managed.id, user_id=user.id, managed_by_provider_id=sso_provider.id))
    db_session.add(UserGroupMember(group_id=manual.id, user_id=user.id, managed_by_provider_id=None))
    await db_session.flush()

    await _resolve(db_session, sso_provider, groups=["map-other"])

    assert await _memberships(db_session, user.id) == {manual.id: None}


async def test_hand_added_membership_is_left_and_not_duplicated(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    sso_provider.sync_groups = True
    dev = await _group(db_session, "dev")
    await _map(db_session, sso_provider, "map-dev", dev)
    user = await _linked(db_session, sso_provider)
    db_session.add(UserGroupMember(group_id=dev.id, user_id=user.id, managed_by_provider_id=None))
    await db_session.flush()

    await _resolve(db_session, sso_provider, groups=["map-dev"])

    assert await _memberships(db_session, user.id) == {dev.id: None}


async def test_membership_managed_by_another_provider_is_left(
    db_session: AsyncSession, sso_provider: SsoProvider, mock_idp: Any
) -> None:
    other = build_sso_provider(mock_idp, slug="map-other-idp", issuer="https://idp-map-other.example.com")
    db_session.add(other)
    await db_session.flush()
    sso_provider.sync_groups = True
    dev = await _group(db_session, "dev")
    await _map(db_session, sso_provider, "map-dev", dev)
    user = await _linked(db_session, sso_provider)
    db_session.add(UserGroupMember(group_id=dev.id, user_id=user.id, managed_by_provider_id=other.id))
    await db_session.flush()

    await _resolve(db_session, sso_provider, groups=[])

    assert await _memberships(db_session, user.id) == {dev.id: other.id}


async def test_missing_claim_keeps_managed_memberships(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.sync_groups = True
    dev = await _group(db_session, "dev")
    await _map(db_session, sso_provider, "map-dev", dev)
    user = await _linked(db_session, sso_provider)
    db_session.add(UserGroupMember(group_id=dev.id, user_id=user.id, managed_by_provider_id=sso_provider.id))
    await db_session.flush()

    await _resolve(db_session, sso_provider)

    assert await _memberships(db_session, user.id) == {dev.id: sso_provider.id}


async def test_invalid_claim_keeps_managed_memberships(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.sync_groups = True
    dev = await _group(db_session, "dev")
    await _map(db_session, sso_provider, "map-dev", dev)
    user = await _linked(db_session, sso_provider)
    db_session.add(UserGroupMember(group_id=dev.id, user_id=user.id, managed_by_provider_id=sso_provider.id))
    await db_session.flush()

    await _resolve(db_session, sso_provider, groups=42)

    assert await _memberships(db_session, user.id) == {dev.id: sso_provider.id}


async def test_sync_off_changes_nothing(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    dev = await _group(db_session, "dev")
    await _map(db_session, sso_provider, "map-dev", dev)
    user = await _linked(db_session, sso_provider)

    await _resolve(db_session, sso_provider, groups=["map-dev"])

    assert await _memberships(db_session, user.id) == {}


async def test_sync_applies_to_jit_and_auto_linked_users(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.sync_groups = True
    sso_provider.allow_jit_provisioning = True
    dev = await _group(db_session, "dev")
    await _map(db_session, sso_provider, "map-dev", dev)

    account = await _resolve(db_session, sso_provider, email="map-jitsync@example.com", groups=["map-dev"])

    assert account.outcome == "provisioned"
    assert await _memberships(db_session, account.user.id) == {dev.id: sso_provider.id}

    sso_provider.link_policy = "auto_verified_email"
    sso_provider.trust_email_verified = True
    target = await _user(db_session, login=_name("autosync"), email="map-autosync@example.com")
    account = await _resolve(
        db_session, sso_provider, sub="map-subject-2", email="map-autosync@example.com", groups=["map-dev"]
    )
    assert account.outcome == "auto_linked"
    assert await _memberships(db_session, target.id) == {dev.id: sso_provider.id}


async def test_group_role_grants_project_permission_after_sync(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    """Project roles held by the group follow the membership in the same DB session (role cache cleared)."""
    sso_provider.sync_groups = True
    project = ProjectFactory.build(is_public=False)
    db_session.add(project)
    role = Role(name=_name("Reporter"), permissions=["view_issues"], builtin=0, issues_visibility="default")
    db_session.add(role)
    dev = await _group(db_session, "dev")
    await db_session.flush()
    member = Member(project_id=project.id, group_id=dev.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await _map(db_session, sso_provider, "map-dev", dev)
    user = await _linked(db_session, sso_provider)

    # Warm the role cache with "no access".
    assert await check_permission(user, project.id, "view_issues", db_session) is False

    await _resolve(db_session, sso_provider, groups=["map-dev"])
    assert await check_permission(user, project.id, "view_issues", db_session) is True

    await _resolve(db_session, sso_provider, groups=[])
    assert await check_permission(user, project.id, "view_issues", db_session) is False


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_mapping_twice_is_a_no_op(
    db_session: AsyncSession, sso_provider: SsoProvider, caplog: pytest.LogCaptureFixture
) -> None:
    sso_provider.sync_groups = True
    sso_provider.sync_admin_flag = True
    sso_provider.admin_groups = ["map-admins"]
    dev = await _group(db_session, "dev")
    await _map(db_session, sso_provider, "map-dev", dev)
    user = await _linked(db_session, sso_provider)
    claims = {"groups": ["map-dev", "map-admins"]}

    with caplog.at_level(logging.INFO, logger="specivo.sso"):
        await _resolve(db_session, sso_provider, **claims)
    assert caplog.text.count("sso.roles.synced") == 1
    membership_ids = set(
        (await db_session.execute(select(UserGroupMember.id).where(UserGroupMember.user_id == user.id))).scalars()
    )

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="specivo.sso"):
        await _resolve(db_session, sso_provider, **claims)

    assert "sso.roles.synced" not in caplog.text
    assert not db_session.new and not db_session.deleted
    assert (
        set((await db_session.execute(select(UserGroupMember.id).where(UserGroupMember.user_id == user.id))).scalars())
        == membership_ids
    )
