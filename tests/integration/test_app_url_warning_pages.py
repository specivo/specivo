"""The APP_URL warning on the pages that show URLs to copy into a forge or an identity provider.

Repository webhook URLs (project Settings, Repositories tab), the SSO redirect
and back-channel logout URIs (provider form, new and edit) and the SCIM base
URL are built from APP_URL. While it points at this machine those pages say
so; when it is a real address opened under another host name they say that.
The request's Host header is only compared: the URLs themselves always come
from APP_URL, never from a forged Host.
"""

from __future__ import annotations

import itertools
import json
from html.parser import HTMLParser

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.app_url import APP_URL_GUIDE_URL
from specivo.core.config import get_settings
from specivo.models.sso import SsoProvider
from specivo.services.auth_service import _make_access_token
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory
from tests.fixtures.forge_hooks import make_integration

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

REAL_URL = "https://specivo.example.com"
REAL_HOST = "specivo.example.com"
FORGED_HOST = "evil.example.net"
LOCAL_URLS = ["http://localhost:8030", "http://127.0.0.1:8000", "http://[::1]:8030", "http://tracker.localhost"]
WARNING = 'data-testid="app-url-warning"'

_counter = itertools.count(1)


def _set_app_url(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr(get_settings(), "app_url", value)


def _reasons(html: str) -> list[str]:
    class _Parser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.reasons: list[str] = []

        def handle_starttag(self, tag, attrs) -> None:
            values = dict(attrs)
            if values.get("data-testid") == "app-url-warning":
                self.reasons.append(values.get("data-reason") or "")

    parser = _Parser()
    parser.feed(html)
    return parser.reasons


@pytest_asyncio.fixture
async def provider(db_session: AsyncSession) -> SsoProvider:
    row = SsoProvider(
        slug=f"appurl-{next(_counter)}",
        display_name="Example Keycloak",
        issuer="https://idp.example.com/realms/test",
        client_id="specivo",
    )
    db_session.add(row)
    await db_session.flush()
    return row


@pytest_asyncio.fixture
async def repo_page(client: AsyncClient, db_session: AsyncSession):
    """A project with one connected repository, and a getter for its settings page as an admin."""
    n = next(_counter)
    admin = AdminUserFactory.build(login=f"appurl_admin_{n}", status="active")
    project = ProjectFactory.build(key=f"AUW{n}", identifier=f"auw-{n}", name="APP_URL warning")
    db_session.add_all([admin, project])
    await db_session.commit()
    integration = await make_integration(db_session, project, provider="github")

    async def _get(host: str | None = None) -> str:
        headers = {"Host": host} if host else {}
        cookies = {"access_token": _make_access_token(admin, get_settings())}
        resp = await client.get(f"/projects/{project.key}/settings/", cookies=cookies, headers=headers)
        assert resp.status_code == 200
        return resp.text

    _get.integration = integration  # type: ignore[attr-defined]
    return _get


async def _sso_page(admin_client: AsyncClient, path: str, host: str | None = None) -> str:
    headers = {"Host": host} if host else {}
    resp = await admin_client.get(path, cookies={"access_token": admin_client.state.token}, headers=headers)
    assert resp.status_code == 200
    return resp.text


def _hook_urls(html: str) -> list[str]:
    start = html.index("repositories: ") + len("repositories: ")
    data = json.loads(html[start : html.index(", providers:", start)])
    return [r["hook_url"] for r in data]


# ---------------------------------------------------------------------------
# Repositories tab
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("app_url", LOCAL_URLS)
async def test_repositories_tab_warns_about_a_local_app_url(repo_page, monkeypatch, app_url: str) -> None:
    _set_app_url(monkeypatch, app_url)
    html = await repo_page()
    assert _reasons(html) == ["local"]
    assert "APP_URL" in html
    assert f"<code>{app_url}</code>" in html
    assert APP_URL_GUIDE_URL in html


async def test_repositories_tab_warns_about_an_empty_app_url(repo_page, monkeypatch) -> None:
    _set_app_url(monkeypatch, "")
    assert _reasons(await repo_page()) == ["unset"]


async def test_repositories_tab_is_quiet_for_a_real_app_url(repo_page, monkeypatch) -> None:
    _set_app_url(monkeypatch, REAL_URL)
    html = await repo_page(REAL_HOST)
    assert WARNING not in html
    assert APP_URL_GUIDE_URL not in html


async def test_repositories_tab_notes_another_host(repo_page, monkeypatch) -> None:
    _set_app_url(monkeypatch, REAL_URL)
    html = await repo_page("other.example.org")
    assert _reasons(html) == ["host_mismatch"]
    assert f"<code>{REAL_URL}</code>" in html
    assert "<code>other.example.org</code>" in html


async def test_hook_url_comes_from_app_url_not_the_host_header(repo_page, monkeypatch) -> None:
    _set_app_url(monkeypatch, REAL_URL)
    html = await repo_page(FORGED_HOST)
    public_id = repo_page.integration.public_id
    assert _hook_urls(html) == [f"{REAL_URL}/hooks/github/{public_id}/"]
    assert f"{FORGED_HOST}/hooks" not in html


# ---------------------------------------------------------------------------
# SSO provider form (new and edit) and its SCIM section
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("app_url", LOCAL_URLS)
async def test_new_sso_provider_warns_about_a_local_app_url(admin_client: AsyncClient, monkeypatch, app_url) -> None:
    _set_app_url(monkeypatch, app_url)
    html = await _sso_page(admin_client, "/admin/sso/new/")
    # Connection section only: a new provider has no SCIM section yet.
    assert _reasons(html) == ["local"]
    assert f"<code>{app_url}</code>" in html


@pytest.mark.parametrize("app_url", LOCAL_URLS)
async def test_sso_provider_warns_in_connection_and_scim(
    admin_client: AsyncClient, provider: SsoProvider, monkeypatch, app_url
) -> None:
    _set_app_url(monkeypatch, app_url)
    html = await _sso_page(admin_client, f"/admin/sso/{provider.id}/")
    assert _reasons(html) == ["local", "local"]
    connection, scim = html.split('data-testid="sso-scim-section"', 1)
    assert WARNING in connection
    assert WARNING in scim


@pytest.mark.parametrize("path", ["/admin/sso/new/", "provider"])
async def test_sso_pages_are_quiet_for_a_real_app_url(
    admin_client: AsyncClient, provider: SsoProvider, monkeypatch, path: str
) -> None:
    _set_app_url(monkeypatch, REAL_URL)
    html = await _sso_page(admin_client, f"/admin/sso/{provider.id}/" if path == "provider" else path, REAL_HOST)
    assert WARNING not in html


async def test_sso_provider_notes_another_host(admin_client: AsyncClient, provider: SsoProvider, monkeypatch) -> None:
    _set_app_url(monkeypatch, REAL_URL)
    html = await _sso_page(admin_client, f"/admin/sso/{provider.id}/", "other.example.org")
    assert _reasons(html) == ["host_mismatch", "host_mismatch"]


async def test_sso_and_scim_urls_come_from_app_url_not_the_host_header(
    admin_client: AsyncClient, provider: SsoProvider, monkeypatch
) -> None:
    _set_app_url(monkeypatch, REAL_URL)
    html = await _sso_page(admin_client, f"/admin/sso/{provider.id}/", FORGED_HOST)
    assert f"{REAL_URL}/auth/oidc/{provider.slug}/callback/" in html
    assert f"{REAL_URL}/auth/oidc/{provider.slug}/backchannel-logout/" in html
    assert f"{REAL_URL}/scim/v2/{provider.slug}" in html
    assert f"{REAL_URL}/auth/oidc/__slug__/callback/" in html
    assert f"{FORGED_HOST}/auth" not in html
    assert f"{FORGED_HOST}/scim" not in html
