"""Web admin pages for SSO providers and the authentication settings card.

The pages are admin-only, show the redirect and back-channel URLs the admin
copies into the identity provider, and never render the client secret.
"""

from __future__ import annotations

import itertools
from html.parser import HTMLParser

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.config import get_settings
from specivo.models.sso import SsoProvider
from specivo.services.sso.auth_settings import get_auth_settings
from specivo.services.sso.provider_service import CLIENT_SECRET_PURPOSE

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

SECRET = "web-page-test-secret-0001"
_counter = itertools.count(1)


@pytest_asyncio.fixture
async def provider(db_session: AsyncSession) -> SsoProvider:
    row = SsoProvider(
        slug=f"spweb-{next(_counter)}",
        display_name="Example Keycloak",
        issuer="https://idp.example.com/realms/test",
        client_id="specivo",
        client_secret_encrypted=secret_box.encrypt(SECRET, CLIENT_SECRET_PURPOSE),
    )
    db_session.add(row)
    await db_session.flush()
    return row


def _cookies(client: AsyncClient) -> dict[str, str]:
    return {"access_token": client.state.token}


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/admin/sso/", "/admin/sso/new/"])
async def test_pages_forbidden_for_non_admin(auth_client: AsyncClient, path: str):
    resp = await auth_client.get(path, cookies=_cookies(auth_client))
    assert resp.status_code == 403


async def test_detail_forbidden_for_non_admin(auth_client: AsyncClient, provider: SsoProvider):
    resp = await auth_client.get(f"/admin/sso/{provider.id}/", cookies=_cookies(auth_client))
    assert resp.status_code == 403


async def test_anonymous_is_redirected_to_login(client: AsyncClient):
    resp = await client.get("/admin/sso/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/login/"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


async def test_list_page_renders(admin_client: AsyncClient, provider: SsoProvider):
    resp = await admin_client.get("/admin/sso/", cookies=_cookies(admin_client))
    assert resp.status_code == 200
    assert provider.display_name in resp.text
    assert f"/admin/sso/{provider.id}/" in resp.text
    assert SECRET not in resp.text
    assert provider.client_secret_encrypted not in resp.text


async def test_delete_dialog_names_the_memberships_it_removes(
    admin_client: AsyncClient, db_session: AsyncSession, provider: SsoProvider
):
    from specivo.models.user_group import UserGroup, UserGroupMember
    from tests.factories.user import UserFactory

    user = UserFactory.build()
    group = UserGroup(name=f"spweb-group-{next(_counter)}")
    db_session.add_all([user, group])
    await db_session.flush()
    db_session.add(UserGroupMember(group_id=group.id, user_id=user.id, managed_by_provider_id=provider.id))
    await db_session.flush()

    resp = await admin_client.get(f"/admin/sso/{provider.id}/", cookies=_cookies(admin_client))
    assert "1 group membership granted by this provider is removed with it." in resp.text

    api = await admin_client.get(f"/api/v1/admin/sso/providers/{provider.id}/")
    assert api.json()["managed_membership_count"] == 1


async def test_detail_page_shows_urls_to_copy(admin_client: AsyncClient, provider: SsoProvider):
    resp = await admin_client.get(f"/admin/sso/{provider.id}/", cookies=_cookies(admin_client))
    assert resp.status_code == 200
    app_url = get_settings().app_url.rstrip("/")
    assert f"{app_url}/auth/oidc/{provider.slug}/callback/" in resp.text
    assert f"{app_url}/auth/oidc/{provider.slug}/backchannel-logout/" in resp.text
    assert SECRET not in resp.text
    assert provider.client_secret_encrypted not in resp.text


async def test_new_page_renders(admin_client: AsyncClient):
    resp = await admin_client.get("/admin/sso/new/", cookies=_cookies(admin_client))
    assert resp.status_code == 200
    assert "adminSsoProvider(" in resp.text


async def test_detail_404_for_unknown(admin_client: AsyncClient):
    resp = await admin_client.get("/admin/sso/987654321/", cookies=_cookies(admin_client))
    assert resp.status_code == 404


async def test_dashboard_links_to_sso(admin_client: AsyncClient):
    resp = await admin_client.get("/admin/", cookies=_cookies(admin_client))
    assert "/admin/sso/" in resp.text


class _XDataGrabber(HTMLParser):
    def __init__(self, component: str) -> None:
        super().__init__()
        self._component = component
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name == "x-data" and value and self._component in value:
                self.values.append(value)


async def test_x_data_attribute_survives_html_parsing(admin_client: AsyncClient, provider: SsoProvider):
    """ADR-0001 section 3: a truncated x-data leaves Alpine failing silently."""
    resp = await admin_client.get(f"/admin/sso/{provider.id}/", cookies=_cookies(admin_client))
    grabber = _XDataGrabber("adminSsoProvider(")
    grabber.feed(resp.text)
    assert grabber.values, "no adminSsoProvider x-data attribute"
    attr = grabber.values[0]
    for key in ("provider:", "userGroups:", "i18n:"):
        assert key in attr
    assert attr.rstrip().endswith("})")


# ---------------------------------------------------------------------------
# Authentication settings card
# ---------------------------------------------------------------------------


async def test_settings_page_has_authentication_card(admin_client: AsyncClient, provider: SsoProvider):
    resp = await admin_client.get("/admin/settings/", cookies=_cookies(admin_client))
    assert resp.status_code == 200
    assert 'id="authentication"' in resp.text
    assert 'name="auto_redirect_provider"' in resp.text
    assert provider.slug in resp.text


async def test_authentication_form_saves(admin_client: AsyncClient, db_session: AsyncSession, provider: SsoProvider):
    resp = await admin_client.post(
        "/admin/settings/authentication/",
        data={
            "hide_local_login": "1",
            "local_login_admins_only": "1",
            "auto_redirect_provider": provider.slug,
            # allow_api_keys_for_sso_users unchecked: absent from the form
        },
        cookies=_cookies(admin_client),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/settings/#authentication"

    current = await get_auth_settings(db_session)
    assert current.hide_local_login is True
    assert current.local_login_admins_only is True
    assert current.auto_redirect_provider == provider.slug
    assert current.allow_api_keys_for_sso_users is False


async def test_authentication_form_refuses_an_admin_api_key(admin_client: AsyncClient, db_session: AsyncSession):
    """The web form is no side door: an admin API key (no session) cannot change sign-in settings."""
    from httpx import ASGITransport

    from specivo.services.api_key_service import ApiKeyService

    _, raw = await ApiKeyService().create_key(db_session, user_id=admin_client.state.user.id, name="admin-agent")  # type: ignore[attr-defined]
    await db_session.flush()
    transport = admin_client._transport
    assert isinstance(transport, ASGITransport)
    async with AsyncClient(transport=transport, base_url="http://test") as key_client:
        resp = await key_client.post(
            "/admin/settings/authentication/",
            data={"hide_local_login": "1", "local_login_admins_only": "1"},
            headers={"Authorization": f"Bearer {raw}"},
            follow_redirects=False,
        )
    assert resp.status_code == 403, resp.text
    current = await get_auth_settings(db_session)
    assert current.hide_local_login is False
    assert current.local_login_admins_only is False


async def test_authentication_form_forbidden_for_non_admin(auth_client: AsyncClient):
    resp = await auth_client.post(
        "/admin/settings/authentication/",
        data={"hide_local_login": "1"},
        cookies=_cookies(auth_client),
        follow_redirects=False,
    )
    assert resp.status_code == 403
