"""Admin API for a provider's SCIM settings: switch, token generate / rotate / revoke.

The token is returned once, by the generate call, and never again. Turning
SCIM on needs a token. Generating, rotating and switching need a signed-in
session (an admin API key gets 403); revoking takes access away and stays
key-accessible.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.sso import SsoProvider
from tests.integration.scim.conftest import ScimClient

pytestmark = pytest.mark.integration


@pytest.fixture
async def provider(db_session: AsyncSession) -> SsoProvider:
    row = SsoProvider(
        slug="scim-admin",
        display_name="SCIM admin",
        enabled=True,
        issuer="https://scim-admin.example.com",
        client_id="specivo",
        token_endpoint_auth_method="none",
    )
    db_session.add(row)
    await db_session.flush()
    return row


def _url(provider: SsoProvider, tail: str = "") -> str:
    return f"/api/v1/admin/sso/providers/{provider.id}/scim/{tail}"


async def test_settings_start_off(admin_client: AsyncClient, provider: SsoProvider) -> None:
    resp = await admin_client.get(_url(provider))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is False
    assert body["token_configured"] is False
    assert body["token_prefix"] is None
    assert body["last_request_at"] is None
    assert body["base_url"].endswith("/scim/v2/scim-admin")
    assert body["user_count"] == 0 and body["group_count"] == 0


async def test_enable_needs_a_token(admin_client: AsyncClient, provider: SsoProvider) -> None:
    resp = await admin_client.patch(_url(provider), json={"enabled": True})
    assert resp.status_code == 400
    assert resp.json()["errors"][0]["code"] == "scim_token_required"


async def test_generate_enable_use_rotate_revoke(
    admin_client: AsyncClient, client: AsyncClient, provider: SsoProvider
) -> None:
    generated = await admin_client.post(_url(provider, "token/"))
    assert generated.status_code == 201, generated.text
    token = generated.json()["token"]
    assert token.startswith("spvscim_")
    assert generated.json()["token_prefix"] == token[:14]

    # Never shown again.
    shown = (await admin_client.get(_url(provider))).json()
    assert token not in str(shown) and shown["token_configured"] is True

    enabled = await admin_client.patch(_url(provider), json={"enabled": True})
    assert enabled.status_code == 200 and enabled.json()["enabled"] is True
    scim = ScimClient(client, provider.slug, token)
    assert (await scim.get("/Users")).status_code == 200
    assert (await admin_client.get(_url(provider))).json()["last_request_at"] is not None

    rotated = (await admin_client.post(_url(provider, "token/"))).json()["token"]
    assert rotated != token
    assert (await scim.get("/Users")).status_code == 401
    assert (await scim.other(token=rotated).get("/Users")).status_code == 200

    revoked = await admin_client.delete(_url(provider, "token/"))
    assert revoked.status_code == 204
    after = (await admin_client.get(_url(provider))).json()
    assert after["enabled"] is False and after["token_configured"] is False
    assert (await scim.other(token=rotated).get("/Users")).status_code == 401


async def test_unknown_provider_is_404(admin_client: AsyncClient) -> None:
    assert (await admin_client.get("/api/v1/admin/sso/providers/999999/scim/")).status_code == 404


async def test_non_admin_is_refused(auth_client: AsyncClient, provider: SsoProvider) -> None:
    assert (await auth_client.get(_url(provider))).status_code == 403
    assert (await auth_client.post(_url(provider, "token/"))).status_code == 403


async def test_reports_skipped_claim_group_sync(
    admin_client: AsyncClient, db_session: AsyncSession, provider: SsoProvider
) -> None:
    provider.sync_groups = True
    await db_session.flush()
    await admin_client.post(_url(provider, "token/"))
    body = (await admin_client.patch(_url(provider), json={"enabled": True})).json()
    assert body["claim_group_sync_skipped"] is True


async def test_provider_page_shows_the_scim_section(
    admin_client: AsyncClient, db_session: AsyncSession, provider: SsoProvider
) -> None:
    token = (await admin_client.post(_url(provider, "token/"))).json()["token"]
    resp = await admin_client.get(f"/admin/sso/{provider.id}/")
    assert resp.status_code == 200
    html = resp.text
    assert "Provisioning (SCIM)" in html
    assert "/scim/v2/scim-admin" in html
    assert 'data-testid="sso-scim-section"' in html
    assert token not in html
    assert "<script>" not in html.split('data-testid="sso-scim-section"', 1)[1].split("</div>", 1)[0]


async def test_new_provider_page_has_no_scim_section(admin_client: AsyncClient) -> None:
    resp = await admin_client.get("/admin/sso/new/")
    assert resp.status_code == 200
    assert 'data-testid="sso-scim-section"' not in resp.text


async def test_external_id_claim_is_a_claim_setting(admin_client: AsyncClient, provider: SsoProvider) -> None:
    url = f"/api/v1/admin/sso/providers/{provider.id}/"
    resp = await admin_client.patch(url, json={"claim_config": {"scim_external_id_claim": "oid"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["claim_config"]["scim_external_id_claim"] == "oid"
    bad = await admin_client.patch(url, json={"claim_config": {"scim_external_id_claim": "not a claim!"}})
    assert bad.status_code == 422
