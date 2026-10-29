"""Web roadmap page integration tests.

Verifies roadmap page renders with versions, progress bars,
and proper auth checks.
"""

from __future__ import annotations

import datetime
import re

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.project import Project
from tests.factories.project import ProjectFactory
from tests.factories.version import VersionFactory

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _project(db_session: AsyncSession) -> Project:
    """Persisted test project for roadmap tests."""
    proj = ProjectFactory.build(key="WRM", identifier="web-roadmap-test")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


# ---------------------------------------------------------------------------
# Tests: roadmap page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_roadmap_page(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _project: Project,
):
    """GET /projects/{key}/roadmap with auth returns 200 and contains 'Roadmap'."""
    # Seed a version
    version = VersionFactory.build(project_id=_project.id, name="v1.0")
    db_session.add(version)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/roadmap/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Roadmap" in resp.text
    assert "v1.0" in resp.text


@pytest.mark.integration
async def test_roadmap_requires_auth(unauth_client: AsyncClient):
    """GET /projects/{key}/roadmap without auth redirects to /login."""
    resp = await unauth_client.get(
        "/projects/ANY/roadmap/",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login/" in resp.headers["location"]


@pytest.mark.integration
async def test_roadmap_empty(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _project: Project,
):
    """GET /projects/{key}/roadmap with no versions shows empty state."""
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/roadmap/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Roadmap" in resp.text


# ---------------------------------------------------------------------------
# Tests: roadmap order, completed toggle, sort select
# ---------------------------------------------------------------------------

_VERSION_NAME = re.compile(r'data-version-name="([^"]+)"')
_D = datetime.date

# A real-world mix: closed releases (some undated) and the open next ones.
_ROADMAP_VERSIONS: list[tuple[str, str, datetime.date | None]] = [
    ("0.10.0", "closed", _D(2026, 10, 1)),
    ("0.2.0", "closed", _D(2026, 5, 20)),
    ("0.11.0", "open", None),
    ("0.1.9", "closed", None),
    ("0.1.10", "closed", _D(2026, 5, 2)),
    ("0.12.0", "locked", _D(2026, 12, 1)),
    ("0.13.0", "open", _D(2027, 2, 1)),
]


@pytest_asyncio.fixture
async def _ordered_versions(db_session: AsyncSession, _project: Project) -> Project:
    for name, status, due in _ROADMAP_VERSIONS:
        db_session.add(VersionFactory.build(project_id=_project.id, name=name, status=status, effective_date=due))
    await db_session.commit()
    return _project


async def _roadmap(client: AsyncClient, project: Project, query: str = "") -> tuple[list[str], list[str], str]:
    resp = await client.get(
        f"/projects/{project.key}/roadmap/{query}",
        cookies={"access_token": client.state.token},
    )
    assert resp.status_code == 200, resp.text
    html = resp.text
    active_html, _, completed_html = html.partition('data-testid="roadmap-completed"')
    return _VERSION_NAME.findall(active_html), _VERSION_NAME.findall(completed_html), html


@pytest.mark.integration
async def test_roadmap_default_order_hides_completed(admin_client: AsyncClient, _ordered_versions: Project):
    """Open and locked versions by due date (undated last); closed ones hidden behind the toggle."""
    active, completed, html = await _roadmap(admin_client, _ordered_versions)
    assert active == ["0.12.0", "0.13.0", "0.11.0"]
    assert completed == []
    assert "Show completed versions (4)" in html
    assert f'href="/projects/{_ordered_versions.key}/roadmap/?show=all"' in html
    # The default sort is preselected in the select.
    assert '<option value="due:asc" selected>' in html


@pytest.mark.integration
async def test_roadmap_completed_toggle(admin_client: AsyncClient, _ordered_versions: Project):
    """show=all lists completed versions in their own section after the active ones."""
    active, completed, html = await _roadmap(admin_client, _ordered_versions, "?show=all")
    assert active == ["0.12.0", "0.13.0", "0.11.0"]
    assert completed == ["0.1.10", "0.2.0", "0.10.0", "0.1.9"]
    assert "Completed versions" in html
    assert "Hide completed versions (4)" in html
    assert 'data-show="all"' in html


@pytest.mark.integration
@pytest.mark.parametrize(
    ("sort", "expected_active", "expected_completed"),
    [
        ("due:asc", ["0.12.0", "0.13.0", "0.11.0"], ["0.1.10", "0.2.0", "0.10.0", "0.1.9"]),
        ("due:desc", ["0.13.0", "0.12.0", "0.11.0"], ["0.10.0", "0.2.0", "0.1.10", "0.1.9"]),
        ("version:asc", ["0.11.0", "0.12.0", "0.13.0"], ["0.1.9", "0.1.10", "0.2.0", "0.10.0"]),
        ("version:desc", ["0.13.0", "0.12.0", "0.11.0"], ["0.10.0", "0.2.0", "0.1.10", "0.1.9"]),
        ("status:asc", ["0.13.0", "0.11.0", "0.12.0"], ["0.1.10", "0.2.0", "0.10.0", "0.1.9"]),
        ("status:desc", ["0.12.0", "0.13.0", "0.11.0"], ["0.1.10", "0.2.0", "0.10.0", "0.1.9"]),
    ],
)
async def test_roadmap_sort_options(
    admin_client: AsyncClient,
    _ordered_versions: Project,
    sort: str,
    expected_active: list[str],
    expected_completed: list[str],
):
    active, completed, html = await _roadmap(admin_client, _ordered_versions, f"?show=all&sort={sort}")
    assert active == expected_active
    assert completed == expected_completed
    assert f'<option value="{sort}" selected>' in html


@pytest.mark.integration
async def test_roadmap_invalid_sort_uses_default(admin_client: AsyncClient, _ordered_versions: Project):
    active, _, _ = await _roadmap(admin_client, _ordered_versions, "?sort=name%3Bdrop")
    assert active == ["0.12.0", "0.13.0", "0.11.0"]


@pytest.mark.integration
async def test_roadmap_remembers_saved_sort(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _ordered_versions: Project,
):
    """The sort saved through the preference endpoint applies to the roadmap and the versions list."""
    resp = await admin_client.patch(
        "/api/v1/users/me/preferences/version-sort/?sort=version:desc",
        headers={"Authorization": f"Bearer {admin_client.state.token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"version_sort": "version:desc"}
    user = admin_client.state.user
    await db_session.refresh(user)
    assert user.preferences["version_sort"] == "version:desc"

    active, _, html = await _roadmap(admin_client, _ordered_versions)
    assert active == ["0.13.0", "0.12.0", "0.11.0"]
    assert '<option value="version:desc" selected>' in html

    # An explicit ?sort= wins for that request without changing the preference.
    active, _, _ = await _roadmap(admin_client, _ordered_versions, "?sort=due:asc")
    assert active == ["0.12.0", "0.13.0", "0.11.0"]
    await db_session.refresh(user)
    assert user.preferences["version_sort"] == "version:desc"

    # The project versions list (settings tab) opens in the saved order too.
    resp = await admin_client.get(
        f"/projects/{_ordered_versions.key}/settings/",
        cookies={"access_token": admin_client.state.token},
    )
    assert resp.status_code == 200, resp.text
    versions_xdata = resp.text.split("projectVersions(", 1)[1].split("i18n:", 1)[0]
    names = re.findall(r'"name": "([^"]+)"', versions_xdata)
    assert names == ["0.13.0", "0.12.0", "0.11.0", "0.10.0", "0.2.0", "0.1.10", "0.1.9"]
    assert 'sort: "version:desc"' in versions_xdata


@pytest.mark.integration
async def test_version_sort_preference_rejects_unknown_value(admin_client: AsyncClient):
    resp = await admin_client.patch(
        "/api/v1/users/me/preferences/version-sort/?sort=name:asc",
        headers={"Authorization": f"Bearer {admin_client.state.token}"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.integration
async def test_version_sort_preference_requires_auth(unauth_client: AsyncClient):
    resp = await unauth_client.patch("/api/v1/users/me/preferences/version-sort/?sort=due:asc")
    assert resp.status_code == 401, resp.text
