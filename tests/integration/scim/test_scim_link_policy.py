"""SCIM follows the provider's own rules for existing accounts and email addresses.

Linking an existing account needs the provider's ``auto_verified_email`` link
policy, ``allowed_email_domains`` applies to every SCIM email, and SCIM never
moves the email of an account that has a local password (a password reset
would then go to an address the IdP chose).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.utils import utcnow
from specivo.models.scim import ScimResource
from specivo.models.user import User
from specivo.testing.factories.user import UserFactory
from tests.integration.scim.conftest import ScimClient, make_scim_provider, user_body

pytestmark = pytest.mark.integration


async def _user(db: AsyncSession, **fields) -> User:
    fields.setdefault("email_verified_at", utcnow())
    user = UserFactory.build(**fields)
    db.add(user)
    await db.flush()
    return user


async def _client_for(client: AsyncClient, db: AsyncSession, slug: str, **overrides) -> ScimClient:
    provider, token = await make_scim_provider(db, slug, **overrides)
    return ScimClient(client, provider.slug, token)


async def test_manual_link_policy_never_links_a_verified_account(client: AsyncClient, db_session: AsyncSession) -> None:
    existing = await _user(db_session, login="sclpkeep", email="sclp-keep@example.com")
    scim = await _client_for(client, db_session, "sclp-manual", link_policy="manual")

    resp = await scim.post("/Users", user_body("sclp-keep@example.com"))

    assert resp.status_code == 409, resp.text
    assert resp.json()["scimType"] == "uniqueness"
    linked = await db_session.scalar(select(ScimResource).where(ScimResource.user_id == existing.id))
    assert linked is None


async def test_manual_link_policy_still_adopts_an_unverified_squatter(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    squatter = await _user(db_session, login="sclpsquat", email="sclp-squat@example.com", email_verified_at=None)
    scim = await _client_for(client, db_session, "sclp-squat", link_policy="manual")

    resp = await scim.post("/Users", user_body("sclp-squat@example.com"))

    assert resp.status_code == 201, resp.text
    await db_session.refresh(squatter)
    assert squatter.password_hash is None


async def test_email_outside_allowed_domains_is_refused_on_create(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    scim = await _client_for(client, db_session, "sclp-domain", allowed_email_domains=["corp.example.com"])

    refused = await scim.post("/Users", user_body("sclp-out@other.example.com"))
    allowed = await scim.post("/Users", user_body("sclp-in@CORP.example.com"))

    assert refused.status_code == 400, refused.text
    assert refused.json()["scimType"] == "invalidValue"
    assert allowed.status_code == 201, allowed.text


async def test_email_outside_allowed_domains_is_refused_on_update(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    scim = await _client_for(client, db_session, "sclp-domain-upd", allowed_email_domains=["corp.example.com"])
    created = (await scim.post("/Users", user_body("sclp-mover@corp.example.com"))).json()

    resp = await scim.put(
        f"/Users/{created['id']}", user_body("sclp-mover@corp.example.com", "sclp-mover@other.example.com")
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["scimType"] == "invalidValue"


async def test_email_of_an_account_with_a_local_password_cannot_change(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    existing = await _user(db_session, login="sclppw", email="sclp-pw@example.com")
    assert existing.password_hash is not None
    scim = await _client_for(client, db_session, "sclp-pw")
    created = (await scim.post("/Users", user_body("sclp-pw-name", "sclp-pw@example.com"))).json()

    resp = await scim.put(f"/Users/{created['id']}", user_body("sclp-pw-name", "sclp-elsewhere@example.com"))

    assert resp.status_code == 400, resp.text
    assert resp.json()["scimType"] == "mutability"
    await db_session.refresh(existing)
    assert existing.email == "sclp-pw@example.com"


async def test_email_of_an_sso_only_account_can_change(client: AsyncClient, db_session: AsyncSession) -> None:
    scim = await _client_for(client, db_session, "sclp-sso")
    created = (await scim.post("/Users", user_body("sclp-sso-name", "sclp-sso@example.com"))).json()

    resp = await scim.put(f"/Users/{created['id']}", user_body("sclp-sso-name", "sclp-sso-new@example.com"))

    assert resp.status_code == 200, resp.text
    assert resp.json()["emails"][0]["value"] == "sclp-sso-new@example.com"
