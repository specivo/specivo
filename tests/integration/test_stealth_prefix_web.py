"""The web UI behind a ``STEALTH_PREFIX``: every URL a page writes stays under it.

The app is created with the prefix set (the settings-override pattern of the
SSO prefix tests), so routes, static files and the manifest are mounted under
it. Each key page is rendered, every root-relative URL in it is collected
(links, forms, assets, htmx and data attributes) and must start with the
prefix, and the links are followed: none may answer 404.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from specivo.core.config import get_settings
from specivo.models.project import EnabledModule
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from specivo.services.wiki_service import WikiService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

PREFIX = "/hidden-ui"
KEY = "STLW"

# Root-relative URLs in the attributes a page uses to reach this instance.
_URL_ATTR = re.compile(
    r"""\s(?:href|src|action|formaction|hx-(?:get|post|put|patch|delete)|data-[\w-]*(?:url|src|href|redirect)|content)"""
    r"""=["'](/[^"']*)["']"""
)


def _local_urls(html: str) -> list[str]:
    return [u for u in _URL_ATTR.findall(html) if not u.startswith("//")]


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> dict[str, Any]:
    status = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Bug", default_status_id=status.id)
    db_session.add(tracker)
    db_session.add(PriorityFactory.build(name="Normal", is_default=True, position=2))
    admin = AdminUserFactory.build(login="stealth_web_admin", status="active")
    project = ProjectFactory.build(key=KEY, identifier="stealth-web")
    db_session.add_all([admin, project])
    await db_session.flush()
    db_session.add_all([EnabledModule(project_id=project.id, name=name) for name in ("issue_tracking", "wiki")])
    await db_session.commit()

    first = await IssueService().create(
        db_session, project, IssueCreate(project_key=KEY, tracker_id=tracker.id, subject="First"), admin
    )
    await db_session.commit()
    description = f"See {first.display_key}, [[Start Page]] and [typed](/issue/{first.display_key}/)."
    second = await IssueService().create(
        db_session,
        project,
        IssueCreate(project_key=KEY, tracker_id=tracker.id, subject="Second", description=description),
        admin,
    )
    page, _content = await WikiService().create_page(
        db_session, project.id, "Start Page", f"Links to {second.display_key}.", admin
    )
    await db_session.commit()
    return {"admin": admin, "first": first.display_key, "second": second.display_key, "slug": page.slug}


@pytest_asyncio.fixture
async def hidden(
    monkeypatch: pytest.MonkeyPatch,
    db_engine: AsyncEngine,
    _test_connection: AsyncConnection,
) -> AsyncGenerator[AsyncClient, None]:
    """A client of an app created with ``STEALTH_PREFIX=/hidden-ui``."""
    from specivo.core.database import get_db
    from specivo.testing.conftest_base import _create_test_app, _make_test_get_db, global_engine_on

    monkeypatch.setenv("STEALTH_PREFIX", PREFIX)
    get_settings.cache_clear()
    try:
        app = _create_test_app()
        app.dependency_overrides[get_db] = _make_test_get_db(_test_connection)
        with global_engine_on(db_engine):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                yield client
    finally:
        monkeypatch.delenv("STEALTH_PREFIX")
        get_settings.cache_clear()


async def _sign_in(client: AsyncClient, login: str) -> None:
    resp = await client.post(f"{PREFIX}/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    client.cookies.set("access_token", resp.json()["access_token"])


def _pages(world: dict[str, Any]) -> list[str]:
    return [
        "/",
        "/projects/",
        f"/projects/{KEY}/",
        f"/projects/{KEY}/issues/",
        f"/projects/{KEY}/issues/new/",
        f"/issue/{world['second']}/",
        f"/issue/{world['second']}/edit/",
        f"/projects/{KEY}/wiki/{world['slug']}/",
        f"/projects/{KEY}/wiki/pages/",
        f"/projects/{KEY}/roadmap/",
        f"/projects/{KEY}/settings/",
        "/search/?q=first",
        "/admin/",
        "/admin/users/",
        "/admin/settings/",
        "/my/profile/",
        "/my/preferences/",
        "/my/api-keys/",
        "/my/ai-usage/",
    ]


async def test_pages_write_only_prefixed_urls_and_none_is_missing(hidden: AsyncClient, world: dict[str, Any]):
    await _sign_in(hidden, world["admin"].login)
    seen: set[str] = set()
    for page in _pages(world):
        resp = await hidden.get(PREFIX + page)
        assert resp.status_code == 200, f"{page}: {resp.status_code}"
        assert f'<meta name="sp-base" content="{PREFIX}">' in resp.text, page
        urls = _local_urls(resp.text)
        assert urls, page
        outside = [u for u in urls if not (u == PREFIX or u.startswith(PREFIX + "/"))]
        assert outside == [], f"{page} writes URLs outside the prefix: {outside}"
        seen.update(u for u in urls if "/logout/" not in u)

    # Follow what the pages link to (GET only; forms are checked by their URL above).
    missing = []
    for url in sorted(seen)[:200]:
        resp = await hidden.get(url)
        if resp.status_code == 404:
            missing.append(url)
    assert missing == [], missing


async def test_rendered_markdown_links_carry_the_prefix(hidden: AsyncClient, world: dict[str, Any]):
    await _sign_in(hidden, world["admin"].login)
    issue = await hidden.get(f"{PREFIX}/issue/{world['second']}/")
    assert f'href="{PREFIX}/issue/{world["first"]}/"' in issue.text
    assert f'href="{PREFIX}/projects/{KEY}/wiki/start-page/"' in issue.text
    assert f'href="/issue/{world["first"]}/"' not in issue.text
    wiki = await hidden.get(f"{PREFIX}/projects/{KEY}/wiki/{world['slug']}/")
    assert f'href="{PREFIX}/issue/{world["second"]}/"' in wiki.text


async def test_a_page_outside_the_prefix_is_not_served(hidden: AsyncClient, world: dict[str, Any]):
    await _sign_in(hidden, world["admin"].login)
    for path in ("/", "/projects/", "/login/", "/static/img/favicon.svg", "/manifest.json", "/health/"):
        resp = await hidden.get(path, headers={"Accept": "text/html"})
        assert resp.status_code == 404, path
        # The bare refusal names neither the prefix nor the product.
        assert PREFIX not in resp.text
        assert "specivo" not in resp.text.lower()


async def test_assets_manifest_and_health_live_under_the_prefix(hidden: AsyncClient):
    assert (await hidden.get(f"{PREFIX}/static/img/favicon.svg")).status_code == 200
    assert (await hidden.get(f"{PREFIX}/static/sw.js")).status_code == 200
    assert (await hidden.get(f"{PREFIX}/health/")).status_code == 200
    manifest = (await hidden.get(f"{PREFIX}/manifest.json")).json()
    assert manifest["start_url"] == f"{PREFIX}/"
    assert manifest["scope"] == f"{PREFIX}/"
    assert all(icon["src"].startswith(f"{PREFIX}/static/") for icon in manifest["icons"])


async def test_signing_in_returns_to_the_page_under_the_prefix(hidden: AsyncClient, world: dict[str, Any]):
    refused = await hidden.get(f"{PREFIX}/projects/{KEY}/issues/", follow_redirects=False)
    assert refused.status_code == 302
    location = urlsplit(refused.headers["location"])
    assert location.path == f"{PREFIX}/login/"
    assert parse_qs(location.query) == {"next": [f"{PREFIX}/projects/{KEY}/issues/"]}

    login = await hidden.get(refused.headers["location"])
    assert login.status_code == 200
    assert f'data-next="{PREFIX}/projects/{KEY}/issues/"' in login.text


async def test_a_next_outside_the_prefix_is_dropped(hidden: AsyncClient, world: dict[str, Any]):
    for bad in ("/projects/", "//evil.example/", "https://evil.example/"):
        login = await hidden.get(f"{PREFIX}/login/", params={"next": bad})
        assert login.status_code == 200
        assert 'data-next=""' in login.text, bad


async def test_form_redirects_stay_under_the_prefix(hidden: AsyncClient, world: dict[str, Any]):
    await _sign_in(hidden, world["admin"].login)
    page = await hidden.get(f"{PREFIX}/my/preferences/")
    token = hidden.cookies.get("csrf_token")
    assert page.status_code == 200 and token
    # The CSRF cookie is Secure; this plain-http client sends it only when set by hand.
    hidden.cookies.delete("csrf_token")
    hidden.cookies.set("csrf_token", token)
    resp = await hidden.post(
        f"{PREFIX}/my/preferences/",
        data={"csrf_token": token, "timezone": "Asia/Bangkok", "language": "th"},
        headers={"X-CSRF-Token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == f"{PREFIX}/my/preferences/"
