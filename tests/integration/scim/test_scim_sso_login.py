"""A SCIM-provisioned user signs in through the same provider's OIDC login (mock IdP).

The first sign-in links the IdP identity to the provisioned account
(``linked_via="scim"``), matched by a trusted, verified email only (a
username claim equal to the SCIM ``userName`` is never enough on its own); ``externalId`` is not assumed
to be the ``sub``. An account another provider provisioned is never linked.
While SCIM is on, the provider's claim-based group sync is skipped.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.scim import ScimResource
from specivo.models.sso import SsoGroupMapping, SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.services.scim.config_service import ScimConfigService
from specivo.testing.oidc_mock import OMIT, MockIdP
from specivo.testing.sso import SsoLoginDriver
from tests.integration.scim.conftest import ScimClient, make_scim_provider, user_body

pytestmark = pytest.mark.integration


@pytest.fixture
async def idp_scim(client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider) -> ScimClient:
    """SCIM on for the mock IdP's provider (``test-idp``)."""
    token = await ScimConfigService().generate_token(db_session, sso_provider, actor=None)
    sso_provider.scim_enabled = True
    await db_session.flush()
    return ScimClient(client, sso_provider.slug, token)


def _sso_error(response: Any) -> str:
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert urlsplit(location).path == "/login/", location
    return parse_qs(urlsplit(location).query)["sso_error"][0]


async def _provision(scim: ScimClient, user_name: str, email: str, **extra: Any) -> User | None:
    resp = await scim.post("/Users", user_body(user_name, email, externalId="not-the-sub", **extra))
    assert resp.status_code == 201, resp.text
    db_user = resp.json()
    return db_user


async def _account(db: AsyncSession, scim_id: str) -> User:
    res = (await db.execute(select(ScimResource).where(ScimResource.scim_id == scim_id))).scalar_one()
    user = await db.get(User, res.user_id)
    await db.refresh(user)
    return user


async def _identity(db: AsyncSession, provider: SsoProvider) -> UserIdentity | None:
    return (await db.execute(select(UserIdentity).where(UserIdentity.provider_id == provider.id))).scalar_one_or_none()


async def test_provisioned_user_signs_in_by_verified_email(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    idp_scim: ScimClient,
    mock_idp: MockIdP,
) -> None:
    sso_provider.trust_email_verified = True
    await db_session.flush()
    created = await _provision(idp_scim, "scim-person", "member@example.com")

    response = await sso_login(client, next_path="/projects/")

    assert response.status_code == 302, response.text
    assert response.headers["location"] == "/projects/"
    identity = await _identity(db_session, sso_provider)
    assert identity is not None and identity.linked_via == "scim"
    assert identity.subject == mock_idp.default_sub
    account = await _account(db_session, created["id"])
    assert identity.user_id == account.id
    assert account.last_login_at is not None

    # The second sign-in uses the identity.
    again = await sso_login(client)
    assert again.status_code == 302 and "sso_error" not in again.headers["location"]


async def test_user_name_and_trusted_email_both_matching_links(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    idp_scim: ScimClient,
) -> None:
    sso_provider.trust_email_verified = True
    await db_session.flush()
    created = await _provision(idp_scim, "Member", "member@example.com")
    response = await sso_login(client)
    assert response.status_code == 302 and "sso_error" not in response.headers["location"], response.headers
    identity = await _identity(db_session, sso_provider)
    assert identity is not None and identity.linked_via == "scim"
    assert identity.user_id == (await _account(db_session, created["id"])).id


async def _users(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(User)) or 0)


@pytest.mark.parametrize(
    ("claims", "trusted"),
    [
        ({"email": OMIT}, True),
        ({"email": "someone-else@example.com"}, True),
        ({}, False),
        ({"email_verified": OMIT}, True),
    ],
    ids=["email-absent", "email-differs", "email-not-trusted", "email-unverified"],
)
async def test_user_name_alone_never_links_or_provisions(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    idp_scim: ScimClient,
    claims: dict[str, Any],
    trusted: bool,
) -> None:
    # The username claim equals a provisioned account's SCIM userName, but the
    # token does not prove the account's email: a user who can pick their own
    # username (or an IdP that sends no email) must not reach someone else's
    # account, and just-in-time provisioning must not create a second one.
    sso_provider.trust_email_verified = trusted
    sso_provider.allow_jit_provisioning = True
    await db_session.flush()
    await _provision(idp_scim, "member", "member@example.com")
    before = await _users(db_session)

    response = await sso_login(client, claims=claims)

    assert _sso_error(response) == "sso_account_not_provisioned"
    assert await _identity(db_session, sso_provider) is None
    assert await _users(db_session) == before


async def test_no_match_is_not_provisioned(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login: SsoLoginDriver, idp_scim
) -> None:
    await _provision(idp_scim, "someone-else", "member@example.com")
    response = await sso_login(client)
    assert _sso_error(response) == "sso_account_not_provisioned"
    assert await _identity(db_session, sso_provider) is None


async def test_deactivated_provisioned_user_cannot_sign_in(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login: SsoLoginDriver, idp_scim
) -> None:
    sso_provider.trust_email_verified = True
    await db_session.flush()
    await _provision(idp_scim, "member", "member@example.com", active=False)
    response = await sso_login(client)
    assert _sso_error(response) == "sso_account_disabled"


async def test_admin_promoted_account_is_not_linked_by_scim_rule(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login: SsoLoginDriver, idp_scim
) -> None:
    sso_provider.trust_email_verified = True
    await db_session.flush()
    created = await _provision(idp_scim, "member", "member@example.com")
    account = await _account(db_session, created["id"])
    account.is_admin = True
    await db_session.flush()
    assert _sso_error(await sso_login(client)) == "sso_account_not_provisioned"


async def test_account_of_another_provider_is_never_linked(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
) -> None:
    # Provider B provisioned the account; A would auto-link by verified email, and must not.
    _other, token_b = await make_scim_provider(db_session, "scim-login-b")
    b = ScimClient(client, "scim-login-b", token_b)
    created = await _provision(b, "member", "member@example.com")
    account = await _account(db_session, created["id"])
    sso_provider.trust_email_verified = True
    sso_provider.link_policy = "auto_verified_email"
    await db_session.flush()

    response = await sso_login(client)

    assert _sso_error(response) == "sso_account_not_provisioned"
    assert (await db_session.execute(select(UserIdentity).where(UserIdentity.user_id == account.id))).first() is None


async def _mapped_group(db: AsyncSession, provider: SsoProvider) -> UserGroup:
    group = UserGroup(name="scim-login-claim-group")
    db.add(group)
    await db.flush()
    db.add(SsoGroupMapping(provider_id=provider.id, idp_group="engineers", user_group_id=group.id))
    provider.sync_groups = True
    provider.trust_email_verified = True
    await db.flush()
    return group


async def _memberships(db: AsyncSession, group: UserGroup) -> int:
    return len((await db.execute(select(UserGroupMember).where(UserGroupMember.group_id == group.id))).all())


async def test_claim_group_sync_is_skipped_while_scim_is_on(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    idp_scim: ScimClient,
) -> None:
    group = await _mapped_group(db_session, sso_provider)
    await _provision(idp_scim, "member", "member@example.com")

    response = await sso_login(client, claims={"groups": ["engineers"]})

    assert response.status_code == 302 and "sso_error" not in response.headers["location"]
    assert await _memberships(db_session, group) == 0


async def test_claim_group_sync_applies_with_scim_off(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    idp_scim: ScimClient,
) -> None:
    group = await _mapped_group(db_session, sso_provider)
    await _provision(idp_scim, "member", "member@example.com")
    sso_provider.scim_enabled = False
    await db_session.flush()

    response = await sso_login(client, claims={"groups": ["engineers"]})

    assert response.status_code == 302 and "sso_error" not in response.headers["location"]
    assert await _memberships(db_session, group) == 1


# ---------------------------------------------------------------------------
# Optional strong match: the SCIM externalId equals a configured claim
# ---------------------------------------------------------------------------


async def test_external_id_claim_links_without_a_trusted_email(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login: SsoLoginDriver, idp_scim
) -> None:
    # Entra ID: SCIM externalId = objectId = the token's oid claim; its email is never trusted.
    sso_provider.claim_config = {**(sso_provider.claim_config or {}), "scim_external_id_claim": "oid"}
    await db_session.flush()
    resp = await idp_scim.post("/Users", user_body("member", "member@example.com", externalId="object-1"))
    created = resp.json()

    response = await sso_login(client, claims={"oid": "object-1", "email": OMIT})

    assert response.status_code == 302 and "sso_error" not in response.headers["location"], response.headers
    identity = await _identity(db_session, sso_provider)
    assert identity is not None and identity.linked_via == "scim"
    assert identity.user_id == (await _account(db_session, created["id"])).id


async def test_external_id_claim_is_off_by_default(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login: SsoLoginDriver, idp_scim
) -> None:
    await idp_scim.post("/Users", user_body("member", "member@example.com", externalId="object-1"))
    response = await sso_login(client, claims={"oid": "object-1", "email": OMIT})
    assert _sso_error(response) == "sso_account_not_provisioned"


async def test_external_id_claim_with_another_value_does_not_link(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login: SsoLoginDriver, idp_scim
) -> None:
    sso_provider.claim_config = {**(sso_provider.claim_config or {}), "scim_external_id_claim": "oid"}
    sso_provider.allow_jit_provisioning = True
    await db_session.flush()
    await idp_scim.post("/Users", user_body("member", "member@example.com", externalId="object-1"))
    response = await sso_login(client, claims={"oid": "object-2", "email": OMIT})
    assert _sso_error(response) == "sso_account_not_provisioned"
    assert await _identity(db_session, sso_provider) is None
