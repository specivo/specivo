"""E2E: the "Provisioning (SCIM)" card of an SSO provider's page.

Generate a token (shown once), turn provisioning on, use the token against the
SCIM endpoint, revoke it. The provider is created and deleted through the admin
API.
"""

from __future__ import annotations

from collections.abc import Generator
from uuid import uuid4

import httpx
import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.e2e]


@pytest.fixture
def provider(api_client: httpx.Client) -> Generator[dict, None, None]:
    slug = f"e2e-scim-{uuid4().hex[:6]}"
    resp = api_client.post(
        "/api/v1/admin/sso/providers/",
        json={
            "slug": slug,
            "display_name": "E2E SCIM",
            "issuer": "https://idp.example.com/e2e",
            "client_id": "specivo",
            "token_endpoint_auth_method": "none",
            "enabled": True,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    yield body
    api_client.delete(f"/api/v1/admin/sso/providers/{body['id']}/")


def test_generate_enable_and_revoke_scim_token(admin_page: Page, provider: dict, e2e_server: str) -> None:
    admin_page.goto(f"/admin/sso/{provider['id']}/")
    section = admin_page.get_by_test_id("sso-scim-section")
    expect(section).to_be_visible()
    expect(section).to_contain_text(f"/scim/v2/{provider['slug']}")
    toggle = section.get_by_test_id("sso-scim-enabled")
    expect(toggle).to_be_disabled()

    section.get_by_role("button", name="Generate token").click()
    token_box = section.get_by_test_id("sso-scim-token")
    expect(token_box).to_be_visible()
    token = token_box.locator("code").inner_text()
    assert token.startswith("spvscim_")

    expect(toggle).to_be_enabled()
    toggle.check()
    expect(toggle).to_be_checked()

    scim = httpx.get(
        f"{e2e_server}/scim/v2/{provider['slug']}/Users", headers={"Authorization": f"Bearer {token}"}, timeout=10
    )
    assert scim.status_code == 200, scim.text

    # The token is not on the page after a reload.
    admin_page.reload()
    expect(admin_page.get_by_test_id("sso-scim-token")).to_have_count(0)
    assert token not in admin_page.content()

    admin_page.once("dialog", lambda dialog: dialog.accept())
    admin_page.get_by_test_id("sso-scim-section").get_by_role("button", name="Revoke token").click()
    expect(admin_page.get_by_test_id("sso-scim-enabled")).not_to_be_checked()
    expect(admin_page.get_by_test_id("sso-scim-enabled")).to_be_disabled()
    scim = httpx.get(
        f"{e2e_server}/scim/v2/{provider['slug']}/Users", headers={"Authorization": f"Bearer {token}"}, timeout=10
    )
    assert scim.status_code == 401
