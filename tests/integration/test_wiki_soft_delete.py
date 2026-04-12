"""Integration tests for wiki page soft-delete API and web routes.

These tests are written TDD-first and will fail until the feature is
implemented. They drive the expected HTTP interface.

Covered:
API endpoints:
- DELETE /api/v1/projects/{key}/wiki/{slug}/  returns 204, page gone from list
- DELETE without delete_wiki_pages permission  returns 403
- DELETE home page  returns 400 or 422
- POST /api/v1/projects/{key}/wiki/{slug}/restore/  returns 200, page back in list
- GET /api/v1/projects/{key}/wiki/trash/  returns list of soft-deleted pages

Web routes:
- GET /projects/{key}/wiki/trash/  returns 200 with deleted page title
- POST /projects/{key}/wiki/{slug}/delete/  redirects to wiki index
- POST /projects/{key}/wiki/trash/{page_id}/restore/  redirects to trash
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.wiki_service import WikiService
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_svc = WikiService()

# ---------------------------------------------------------------------------
# Helpers shared across fixtures
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession, login: str = "wsd_user") -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client: AsyncClient, login: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _make_project(db: AsyncSession, key: str = "WSD", identifier: str = "wiki-soft-del") -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


async def _enable_wiki(db: AsyncSession, project: Project) -> None:
    db.add(EnabledModule(project_id=project.id, name="wiki"))
    await db.commit()


async def _add_member_with_permissions(
    db: AsyncSession,
    project: Project,
    user: User,
    permissions: list[str],
) -> None:
    role = Role(
        name=f"TestRole-{project.key}-{user.id}",
        permissions=permissions,
        builtin=0,
    )
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    mr = MemberRole(member_id=member.id, role_id=role.id)
    db.add(mr)
    await db.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = await _make_project(db_session)
    await _enable_wiki(db_session, proj)
    return proj


@pytest_asyncio.fixture
async def wiki_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="wiki_soft_del_user")


@pytest_asyncio.fixture
async def authed_client(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
    wiki_user: User,
) -> AsyncClient:
    """Client authenticated as a manager with full wiki permissions."""
    await _add_member_with_permissions(
        db_session,
        project,
        wiki_user,
        ["view_wiki", "manage_wiki", "delete_wiki_pages"],
    )
    token = await _login(client, wiki_user.login)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest_asyncio.fixture
async def viewer_client(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
) -> AsyncClient:
    """Client authenticated as a view-only member (no delete_wiki_pages)."""
    viewer = await _make_user(db_session, login="wiki_sd_viewer")
    await _add_member_with_permissions(db_session, project, viewer, ["view_wiki", "manage_wiki"])
    token = await _login(client, viewer.login)
    # Return a fresh client with the viewer token — we cannot mutate the shared
    # client headers without affecting other fixtures, so we copy the header.
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------------------------------------------------------------------------
# Helper: create a page via the API
# ---------------------------------------------------------------------------


async def _api_create_page(client: AsyncClient, project_key: str, title: str, text: str = "content") -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project_key}/wiki/",
        json={"title": title, "text": text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# API: DELETE /api/v1/projects/{key}/wiki/{slug}/
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_api_soft_delete_returns_204(
    authed_client: AsyncClient,
    project: Project,
):
    page = await _api_create_page(authed_client, project.key, "Delete Via API")

    resp = await authed_client.delete(f"/api/v1/projects/{project.key}/wiki/{page['slug']}/")
    assert resp.status_code == 204


@pytest.mark.integration
async def test_api_soft_delete_page_disappears_from_list(
    authed_client: AsyncClient,
    project: Project,
):
    page = await _api_create_page(authed_client, project.key, "Disappears From List")

    await authed_client.delete(f"/api/v1/projects/{project.key}/wiki/{page['slug']}/")

    resp = await authed_client.get(f"/api/v1/projects/{project.key}/wiki/")
    assert resp.status_code == 200
    slugs = [item["slug"] for item in resp.json()["items"]]
    assert page["slug"] not in slugs


@pytest.mark.integration
async def test_api_soft_delete_page_returns_404_on_get(
    authed_client: AsyncClient,
    project: Project,
):
    page = await _api_create_page(authed_client, project.key, "404 After Delete")

    await authed_client.delete(f"/api/v1/projects/{project.key}/wiki/{page['slug']}/")

    resp = await authed_client.get(f"/api/v1/projects/{project.key}/wiki/{page['slug']}/")
    assert resp.status_code == 404


@pytest.mark.integration
async def test_api_soft_delete_requires_delete_permission(
    viewer_client: AsyncClient,
    project: Project,
):
    """Member without delete_wiki_pages permission receives 403."""
    # Create the page as a viewer first — but viewers have manage_wiki so creation is fine
    page = await _api_create_page(viewer_client, project.key, "No Delete Permission Page")

    resp = await viewer_client.delete(f"/api/v1/projects/{project.key}/wiki/{page['slug']}/")
    assert resp.status_code == 403


@pytest.mark.integration
async def test_api_soft_delete_home_page_returns_error(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    wiki_user: User,
):
    """Deleting the home page must return 400 or 422."""
    home = await _svc.ensure_home_page(db_session, project.id, wiki_user)
    await db_session.commit()

    resp = await authed_client.delete(f"/api/v1/projects/{project.key}/wiki/{home.slug}/")
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# API: POST /api/v1/projects/{key}/wiki/{slug}/restore/
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_api_restore_page_returns_200(
    authed_client: AsyncClient,
    project: Project,
):
    page = await _api_create_page(authed_client, project.key, "Restore Via API")
    await authed_client.delete(f"/api/v1/projects/{project.key}/wiki/{page['slug']}/")

    resp = await authed_client.post(f"/api/v1/projects/{project.key}/wiki/{page['slug']}/restore/")
    assert resp.status_code == 200


@pytest.mark.integration
async def test_api_restore_page_reappears_in_list(
    authed_client: AsyncClient,
    project: Project,
):
    page = await _api_create_page(authed_client, project.key, "Back In List")
    await authed_client.delete(f"/api/v1/projects/{project.key}/wiki/{page['slug']}/")
    await authed_client.post(f"/api/v1/projects/{project.key}/wiki/{page['slug']}/restore/")

    resp = await authed_client.get(f"/api/v1/projects/{project.key}/wiki/")
    assert resp.status_code == 200
    slugs = [item["slug"] for item in resp.json()["items"]]
    assert page["slug"] in slugs


@pytest.mark.integration
async def test_api_restore_page_returns_page_data(
    authed_client: AsyncClient,
    project: Project,
):
    page = await _api_create_page(authed_client, project.key, "Restored Data")
    await authed_client.delete(f"/api/v1/projects/{project.key}/wiki/{page['slug']}/")

    resp = await authed_client.post(f"/api/v1/projects/{project.key}/wiki/{page['slug']}/restore/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["slug"] == page["slug"]
    assert data["title"] == page["title"]


# ---------------------------------------------------------------------------
# API: GET /api/v1/projects/{key}/wiki/trash/
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_api_trash_lists_deleted_pages(
    authed_client: AsyncClient,
    project: Project,
):
    page = await _api_create_page(authed_client, project.key, "In The Trash")
    await authed_client.delete(f"/api/v1/projects/{project.key}/wiki/{page['slug']}/")

    resp = await authed_client.get(f"/api/v1/projects/{project.key}/wiki/trash/")
    assert resp.status_code == 200
    items = resp.json()["items"]
    slugs = [item["slug"] for item in items]
    assert page["slug"] in slugs


@pytest.mark.integration
async def test_api_trash_does_not_include_active_pages(
    authed_client: AsyncClient,
    project: Project,
):
    active = await _api_create_page(authed_client, project.key, "Still Active")
    deleted = await _api_create_page(authed_client, project.key, "Trashed Page")
    await authed_client.delete(f"/api/v1/projects/{project.key}/wiki/{deleted['slug']}/")

    resp = await authed_client.get(f"/api/v1/projects/{project.key}/wiki/trash/")
    assert resp.status_code == 200
    items = resp.json()["items"]
    slugs = [item["slug"] for item in items]
    assert deleted["slug"] in slugs
    assert active["slug"] not in slugs


@pytest.mark.integration
async def test_api_trash_empty_when_no_deletes(
    authed_client: AsyncClient,
    project: Project,
):
    await _api_create_page(authed_client, project.key, "No Deletes Here")

    resp = await authed_client.get(f"/api/v1/projects/{project.key}/wiki/trash/")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


# ---------------------------------------------------------------------------
# Web: GET /projects/{key}/wiki/trash/
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _wiki_project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="WSDW", identifier="web-wiki-soft-del")
    db_session.add(proj)
    await db_session.flush()
    db_session.add(EnabledModule(project_id=proj.id, name="wiki"))
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest.mark.integration
async def test_web_trash_page_returns_200(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    user = admin_client.state.user
    page, _content = await _svc.create_page(
        db_session, _wiki_project.id, "Web Trash Page", "content", user
    )
    await db_session.commit()
    await db_session.refresh(page)

    await _svc.delete_page(db_session, page.id, deleted_by=user)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_wiki_project.key}/wiki/trash/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Web Trash Page" in resp.text


@pytest.mark.integration
async def test_web_trash_page_shows_deleted_page_titles(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    user = admin_client.state.user
    page, _ = await _svc.create_page(
        db_session, _wiki_project.id, "Trash Title Check", "content", user
    )
    await db_session.commit()
    await db_session.refresh(page)

    await _svc.delete_page(db_session, page.id, deleted_by=user)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_wiki_project.key}/wiki/trash/",
        cookies={"access_token": token},
    )
    assert "Trash Title Check" in resp.text


@pytest.mark.integration
async def test_web_trash_requires_auth(unauth_client: AsyncClient, _wiki_project: Project):
    """GET /projects/{key}/wiki/trash without auth redirects to login."""
    resp = await unauth_client.get(
        f"/projects/{_wiki_project.key}/wiki/trash/",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login/" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Web: POST /projects/{key}/wiki/{slug}/delete/
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_web_delete_redirects_to_wiki_index(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    user = admin_client.state.user
    page, _ = await _svc.create_page(
        db_session, _wiki_project.id, "Web Delete Me", "content", user
    )
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.post(
        f"/projects/{_wiki_project.key}/wiki/{page.slug}/delete/",
        cookies={"access_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    # Should redirect to the wiki index or list
    assert f"/projects/{_wiki_project.key}/wiki" in resp.headers["location"]


@pytest.mark.integration
async def test_web_delete_removes_page_from_wiki_list(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    user = admin_client.state.user
    page, _ = await _svc.create_page(
        db_session, _wiki_project.id, "Web Delete Invisible", "content", user
    )
    await db_session.commit()

    token = admin_client.state.token
    await admin_client.post(
        f"/projects/{_wiki_project.key}/wiki/{page.slug}/delete/",
        cookies={"access_token": token},
        follow_redirects=True,
    )

    # The page should no longer appear in the wiki index listing
    list_resp = await admin_client.get(
        f"/projects/{_wiki_project.key}/wiki/",
        cookies={"access_token": token},
        follow_redirects=True,
    )
    assert "Web Delete Invisible" not in list_resp.text


# ---------------------------------------------------------------------------
# Web: POST /projects/{key}/wiki/trash/{page_id}/restore/
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_web_restore_redirects_to_trash(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    user = admin_client.state.user
    page, _ = await _svc.create_page(
        db_session, _wiki_project.id, "Web Restore Me", "content", user
    )
    await db_session.commit()
    await db_session.refresh(page)

    await _svc.delete_page(db_session, page.id, deleted_by=user)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.post(
        f"/projects/{_wiki_project.key}/wiki/trash/{page.id}/restore/",
        cookies={"access_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "trash" in resp.headers["location"]


@pytest.mark.integration
async def test_web_restore_page_reappears_in_wiki(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    user = admin_client.state.user
    page, _ = await _svc.create_page(
        db_session, _wiki_project.id, "Web Restored Page", "content", user
    )
    await db_session.commit()
    await db_session.refresh(page)

    await _svc.delete_page(db_session, page.id, deleted_by=user)
    await db_session.commit()

    token = admin_client.state.token
    await admin_client.post(
        f"/projects/{_wiki_project.key}/wiki/trash/{page.id}/restore/",
        cookies={"access_token": token},
        follow_redirects=True,
    )

    # The page must be findable again via the API
    get_resp = await admin_client.get(
        f"/projects/{_wiki_project.key}/wiki/{page.slug}/",
        cookies={"access_token": token},
    )
    assert get_resp.status_code == 200
    assert "Web Restored Page" in get_resp.text
