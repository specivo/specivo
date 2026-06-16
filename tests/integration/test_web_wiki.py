"""Web wiki page integration tests.

Verifies wiki index, show, edit, and history pages render correctly
with proper auth checks and content.
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
# Shared fixtures
# ---------------------------------------------------------------------------

_wiki_svc = WikiService()


@pytest_asyncio.fixture
async def _wiki_project(db_session: AsyncSession) -> Project:
    """Persisted test project with wiki module enabled."""
    proj = ProjectFactory.build(key="WWK", identifier="web-wiki-test")
    db_session.add(proj)
    await db_session.flush()

    module = EnabledModule(project_id=proj.id, name="wiki")
    db_session.add(module)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


# ---------------------------------------------------------------------------
# Tests: wiki index page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wiki_index(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    """GET /projects/{key}/wiki redirects to /wiki/home/."""
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_wiki_project.key}/wiki/",
        cookies={"access_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/wiki/home/" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Tests: wiki show page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wiki_show_page(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    """Create a wiki page, GET /projects/{key}/wiki/{slug} returns 200."""
    user = admin_client.state.user
    page, _content = await _wiki_svc.create_page(db_session, _wiki_project.id, "Test Page", "Hello world", user)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_wiki_project.key}/wiki/{page.slug}/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Test Page" in resp.text
    assert "Hello world" in resp.text


# ---------------------------------------------------------------------------
# Tests: wiki edit page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wiki_edit_page(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    """GET /projects/{key}/wiki/{slug}/edit returns 200 with edit form."""
    user = admin_client.state.user
    page, _content = await _wiki_svc.create_page(db_session, _wiki_project.id, "Edit Test", "Initial content", user)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_wiki_project.key}/wiki/{page.slug}/edit/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Edit Test" in resp.text


@pytest.mark.integration
async def test_wiki_edit_page_uses_markdown_editor(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    """Wiki edit page wires the EasyMDE-based markdownEditor wrapper.

    The wrapper must:
      - Be registered as an Alpine component named ``markdownEditor``.
      - Point at the server-side preview endpoint ``/api/v1/markdown/preview/``.
      - Pass ``context: "wiki"`` so the server applies wiki rendering rules.
      - Expose a paste/drop hook to the parent ``wikiForm`` for image upload.
    """
    user = admin_client.state.user
    page, _content = await _wiki_svc.create_page(db_session, _wiki_project.id, "Editor Wrapper Test", "body", user)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_wiki_project.key}/wiki/{page.slug}/edit/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "markdownEditor(" in body
    assert "/api/v1/markdown/preview/" in body
    assert 'data-md-context="wiki"' in body
    assert "handleEditorPasteDrop" in body
    # The vendored EasyMDE asset must be loaded on the page.
    assert "easymde.2.18.0.min.js" in body
    assert "easymde.2.18.0.min.css" in body


# ---------------------------------------------------------------------------
# Tests: wiki history page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wiki_history(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    """GET /projects/{key}/wiki/{slug}/history returns 200 with version info."""
    user = admin_client.state.user
    page, _content = await _wiki_svc.create_page(db_session, _wiki_project.id, "History Test", "Version one", user)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_wiki_project.key}/wiki/{page.slug}/history/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "History" in resp.text


# ---------------------------------------------------------------------------
# Tests: wiki diff page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wiki_diff_page(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    """GET /projects/{key}/wiki/{slug}/diff/?from_version=1&to_version=2 shows diff."""
    user = admin_client.state.user
    page, _content = await _wiki_svc.create_page(
        db_session,
        _wiki_project.id,
        "Diff Test",
        "Original text",
        user,
    )
    await db_session.commit()
    await db_session.refresh(page)

    # Create version 2 by updating
    await _wiki_svc.update_page(db_session, page.id, "Updated text", user, page.lock_version)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_wiki_project.key}/wiki/{page.slug}/diff/?from_version=1&to_version=2",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Comparing version" in resp.text
    assert "Original text" in resp.text or "-Original text" in resp.text
    assert "Updated text" in resp.text or "+Updated text" in resp.text


@pytest.mark.integration
async def test_wiki_diff_no_changes(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    """Diffing the same version shows 'No differences'."""
    user = admin_client.state.user
    page, _content = await _wiki_svc.create_page(
        db_session,
        _wiki_project.id,
        "Same Diff",
        "Same content",
        user,
    )
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_wiki_project.key}/wiki/{page.slug}/diff/?from_version=1&to_version=1",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "No differences" in resp.text


# ---------------------------------------------------------------------------
# Tests: auth required
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wiki_requires_auth(unauth_client: AsyncClient):
    """GET /projects/{key}/wiki without auth redirects to /login."""
    resp = await unauth_client.get(
        "/projects/ANY/wiki/",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login/" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Helpers for permission tests
# ---------------------------------------------------------------------------


async def _make_restricted_user(db_session: AsyncSession, login: str = "restricted_wiki_user") -> User:
    """Create and persist a non-admin user."""
    user = UserFactory.build(login=login, status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _add_member_with_permissions(
    db_session: AsyncSession,
    project: Project,
    user: User,
    permissions: list[str],
) -> None:
    """Add user to project with an explicit permission list (not wildcard)."""
    role = Role(
        name=f"TestRole-{project.key}-{user.id}",
        permissions=permissions,
        builtin=0,
    )
    db_session.add(role)
    await db_session.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    mr = MemberRole(member_id=member.id, role_id=role.id)
    db_session.add(mr)
    await db_session.commit()


async def _get_token(client: AsyncClient, login: str) -> str:
    """Login and return the JWT access token."""
    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Tests: wiki_new with ?parent= query param
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wiki_new_with_parent_param(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    """GET /projects/{key}/wiki/new/?parent={id} pre-selects the parent page."""
    user = admin_client.state.user
    parent_page, _ = await _wiki_svc.create_page(db_session, _wiki_project.id, "Parent For New", "parent content", user)
    await db_session.commit()
    await db_session.refresh(parent_page)

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_wiki_project.key}/wiki/new/?parent={parent_page.id}",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200, resp.text
    # The parent's slug must appear in the rendered HTML (pre-selected in the picker)
    assert parent_page.slug in resp.text


# ---------------------------------------------------------------------------
# Tests: view_wiki permission on wiki show page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wiki_show_permission_denied(
    admin_client: AsyncClient,
    client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    """Member without view_wiki permission gets 403 on wiki show page."""
    # Create the page as admin so it exists
    admin_user = admin_client.state.user
    page, _ = await _wiki_svc.create_page(db_session, _wiki_project.id, "Secret Page", "secret content", admin_user)
    await db_session.commit()

    # Create a restricted member with NO wiki permissions
    restricted_user = await _make_restricted_user(db_session, login="no_view_wiki_user")
    await _add_member_with_permissions(db_session, _wiki_project, restricted_user, ["view_issues"])

    token = await _get_token(client, restricted_user.login)
    resp = await client.get(
        f"/projects/{_wiki_project.key}/wiki/{page.slug}/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Tests: manage_wiki permission on wiki edit page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wiki_edit_permission_denied(
    admin_client: AsyncClient,
    client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    """Member with view_wiki but not manage_wiki gets 403 on wiki edit page."""
    admin_user = admin_client.state.user
    page, _ = await _wiki_svc.create_page(db_session, _wiki_project.id, "Editable Page", "some content", admin_user)
    await db_session.commit()

    # Create a member with view_wiki only (no manage_wiki)
    viewer_user = await _make_restricted_user(db_session, login="view_only_wiki_user")
    await _add_member_with_permissions(db_session, _wiki_project, viewer_user, ["view_wiki"])

    token = await _get_token(client, viewer_user.login)
    resp = await client.get(
        f"/projects/{_wiki_project.key}/wiki/{page.slug}/edit/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Tests: can_manage_wiki template context (Edit button visibility)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wiki_show_no_edit_button_without_manage_permission(
    admin_client: AsyncClient,
    client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    """Wiki show page must not contain the edit URL for a view-only member."""
    admin_user = admin_client.state.user
    page, _ = await _wiki_svc.create_page(db_session, _wiki_project.id, "View Only Page", "read this", admin_user)
    await db_session.commit()
    await db_session.refresh(page)

    # Member with view_wiki but no manage_wiki
    viewer_user = await _make_restricted_user(db_session, login="view_only_no_edit_user")
    await _add_member_with_permissions(db_session, _wiki_project, viewer_user, ["view_wiki"])

    token = await _get_token(client, viewer_user.login)
    resp = await client.get(
        f"/projects/{_wiki_project.key}/wiki/{page.slug}/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200, resp.text
    # The edit endpoint URL must not appear in the rendered page
    assert f"/wiki/{page.slug}/edit/" not in resp.text
