"""Integration tests for the Wiki API.

Covers:
- Wiki page CRUD (create, get, update, delete, list)
- Content versioning (history, specific version)
- Page hierarchy (parent_slug)
- Optimistic locking (409 on stale lock_version)
- Redirect on rename (old slug -> new page)
- Module gate (403 when wiki module disabled)
- Duplicate slug conflict (409)
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
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession, login: str = "wiki_user") -> User:
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


async def _make_project(db: AsyncSession, key: str = "WIKI", identifier: str = "wiki-project") -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


async def _enable_wiki(db: AsyncSession, project: Project) -> None:
    db.add(EnabledModule(project_id=project.id, name="wiki"))
    await db.commit()


async def _add_manager(db: AsyncSession, project: Project, user: User) -> None:
    """Add user as a member with manage_wiki permission (Manager role)."""
    role = Role(
        name=f"Manager-{project.key}-{user.id}",
        permissions=["*"],
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
    return await _make_project(db_session)


@pytest_asyncio.fixture
async def wiki_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="wiki_test_user")


@pytest_asyncio.fixture
async def authed_client(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
    wiki_user: User,
) -> AsyncClient:
    """Client authenticated as a manager with wiki module enabled."""
    await _enable_wiki(db_session, project)
    await _add_manager(db_session, project, wiki_user)
    token = await _login(client, wiki_user.login)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_wiki_page(
    authed_client: AsyncClient,
    project: Project,
):
    resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Getting Started", "text": "Hello world"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["title"] == "Getting Started"
    assert data["slug"] == "getting-started"
    assert data["text"] == "Hello world"
    assert data["content_version"] == 1
    assert data["lock_version"] >= 0


@pytest.mark.asyncio
async def test_get_wiki_page(
    authed_client: AsyncClient,
    project: Project,
):
    # Create a page first
    await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Test Page", "text": "Some content"},
    )
    # GET by slug
    resp = await authed_client.get(f"/api/v1/projects/{project.key}/wiki/test-page/")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["title"] == "Test Page"
    assert data["text"] == "Some content"
    assert data["content_version"] == 1


@pytest.mark.asyncio
async def test_update_wiki_page_creates_new_version(
    authed_client: AsyncClient,
    project: Project,
):
    # Create
    create_resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Versioned", "text": "v1"},
    )
    assert create_resp.status_code == 201
    lock_version = create_resp.json()["lock_version"]

    # Update
    resp = await authed_client.patch(
        f"/api/v1/projects/{project.key}/wiki/versioned/",
        json={"text": "v2", "lock_version": lock_version, "comments": "second edit"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["text"] == "v2"
    assert data["content_version"] == 2


@pytest.mark.asyncio
async def test_delete_wiki_page(
    authed_client: AsyncClient,
    project: Project,
):
    await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "To Delete", "text": "bye"},
    )
    resp = await authed_client.delete(f"/api/v1/projects/{project.key}/wiki/to-delete/")
    assert resp.status_code == 204

    # Confirm gone
    resp2 = await authed_client.get(f"/api/v1/projects/{project.key}/wiki/to-delete/")
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_list_wiki_pages(
    authed_client: AsyncClient,
    project: Project,
):
    await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Page A", "text": "a"},
    )
    await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Page B", "text": "b"},
    )

    resp = await authed_client.get(f"/api/v1/projects/{project.key}/wiki/")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 2
    slugs = {item["slug"] for item in items}
    assert slugs == {"page-a", "page-b"}


@pytest.mark.asyncio
async def test_wiki_page_history(
    authed_client: AsyncClient,
    project: Project,
):
    # Create + 2 updates = 3 versions
    create_resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "History", "text": "v1"},
    )
    lv = create_resp.json()["lock_version"]

    resp2 = await authed_client.patch(
        f"/api/v1/projects/{project.key}/wiki/history/",
        json={"text": "v2", "lock_version": lv},
    )
    lv2 = resp2.json()["lock_version"]

    await authed_client.patch(
        f"/api/v1/projects/{project.key}/wiki/history/",
        json={"text": "v3", "lock_version": lv2},
    )

    resp = await authed_client.get(f"/api/v1/projects/{project.key}/wiki/history/versions/")
    assert resp.status_code == 200, resp.text
    versions = resp.json()["versions"]
    assert len(versions) == 3
    # Versions are returned newest first
    assert versions[0]["version"] == 3
    assert versions[2]["version"] == 1


@pytest.mark.asyncio
async def test_wiki_page_specific_version(
    authed_client: AsyncClient,
    project: Project,
):
    create_resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Specific", "text": "original"},
    )
    lv = create_resp.json()["lock_version"]

    await authed_client.patch(
        f"/api/v1/projects/{project.key}/wiki/specific/",
        json={"text": "updated", "lock_version": lv},
    )

    # Get version 1
    resp = await authed_client.get(f"/api/v1/projects/{project.key}/wiki/specific/versions/1/")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["version"] == 1
    assert data["text"] == "original"


@pytest.mark.asyncio
async def test_wiki_page_hierarchy(
    authed_client: AsyncClient,
    project: Project,
):
    # Create parent
    parent_resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Parent Page", "text": "parent"},
    )
    assert parent_resp.status_code == 201

    # Create child with parent_slug
    child_resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Child Page", "text": "child", "parent_slug": "parent-page"},
    )
    assert child_resp.status_code == 201, child_resp.text
    child = child_resp.json()
    assert child["parent_id"] == parent_resp.json()["id"]


@pytest.mark.asyncio
async def test_wiki_page_optimistic_lock(
    authed_client: AsyncClient,
    project: Project,
):
    create_resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Locked", "text": "v1"},
    )
    assert create_resp.status_code == 201

    # Use a wrong lock_version
    resp = await authed_client.patch(
        f"/api/v1/projects/{project.key}/wiki/locked/",
        json={"text": "v2", "lock_version": 999},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_wiki_redirect_on_rename(
    authed_client: AsyncClient,
    project: Project,
):
    create_resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Old Name", "text": "content"},
    )
    lv = create_resp.json()["lock_version"]

    rename_resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/old-name/rename/",
        json={"title": "New Name", "lock_version": lv},
    )
    assert rename_resp.status_code == 200, rename_resp.text
    data = rename_resp.json()
    assert data["slug"] == "new-name"
    assert data["title"] == "New Name"


@pytest.mark.asyncio
async def test_wiki_redirect_follow(
    authed_client: AsyncClient,
    project: Project,
):
    create_resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Original Title", "text": "content"},
    )
    lv = create_resp.json()["lock_version"]

    await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/original-title/rename/",
        json={"title": "Renamed Title", "lock_version": lv},
    )

    # Access by old slug -> should follow redirect
    resp = await authed_client.get(f"/api/v1/projects/{project.key}/wiki/original-title/")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["slug"] == "renamed-title"
    assert data["title"] == "Renamed Title"


@pytest.mark.asyncio
async def test_wiki_page_not_found(
    authed_client: AsyncClient,
    project: Project,
):
    resp = await authed_client.get(f"/api/v1/projects/{project.key}/wiki/nonexistent/")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_wiki_requires_module_enabled(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
    wiki_user: User,
):
    """Wiki module NOT enabled -> 403."""
    # Add manager role but do NOT enable wiki module
    await _add_manager(db_session, project, wiki_user)
    token = await _login(client, wiki_user.login)
    client.headers["Authorization"] = f"Bearer {token}"

    resp = await client.get(f"/api/v1/projects/{project.key}/wiki/")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_wiki_duplicate_slug(
    authed_client: AsyncClient,
    project: Project,
):
    resp1 = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Unique Title", "text": "first"},
    )
    assert resp1.status_code == 201

    resp2 = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Unique Title", "text": "duplicate"},
    )
    assert resp2.status_code == 409


# ---------------------------------------------------------------------------
# Tests: PATCH title / parent_slug (new wiki PATCH features)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_wiki_page_rename_via_patch(
    authed_client: AsyncClient,
    project: Project,
):
    """PATCH with title renames the page and creates a redirect from the old slug."""
    create_resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Old Title", "text": "original text"},
    )
    assert create_resp.status_code == 201, create_resp.text
    lock_version = create_resp.json()["lock_version"]

    patch_resp = await authed_client.patch(
        f"/api/v1/projects/{project.key}/wiki/old-title/",
        json={"title": "New Title", "text": "original text", "lock_version": lock_version},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    data = patch_resp.json()
    assert data["slug"] == "new-title"
    assert data["title"] == "New Title"

    # GET by old slug should follow redirect and return the renamed page
    redirect_resp = await authed_client.get(f"/api/v1/projects/{project.key}/wiki/old-title/")
    assert redirect_resp.status_code == 200, redirect_resp.text
    redirect_data = redirect_resp.json()
    assert redirect_data["slug"] == "new-title"
    assert redirect_data["title"] == "New Title"


@pytest.mark.asyncio
async def test_update_wiki_page_set_parent(
    authed_client: AsyncClient,
    project: Project,
):
    """PATCH with parent_slug sets the parent of a page."""
    parent_resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Parent Page", "text": "parent content"},
    )
    assert parent_resp.status_code == 201, parent_resp.text
    parent_id = parent_resp.json()["id"]

    child_resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Child Page", "text": "child content"},
    )
    assert child_resp.status_code == 201, child_resp.text
    child_lock_version = child_resp.json()["lock_version"]
    assert child_resp.json()["parent_id"] is None

    patch_resp = await authed_client.patch(
        f"/api/v1/projects/{project.key}/wiki/child-page/",
        json={
            "text": "child content",
            "lock_version": child_lock_version,
            "parent_slug": "parent-page",
        },
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json()["parent_id"] == parent_id


@pytest.mark.asyncio
async def test_update_wiki_page_remove_parent(
    authed_client: AsyncClient,
    project: Project,
):
    """PATCH with empty string parent_slug moves the page to root (no parent)."""
    parent_resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Root Parent", "text": "parent"},
    )
    assert parent_resp.status_code == 201, parent_resp.text

    child_resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Nested Child", "text": "child", "parent_slug": "root-parent"},
    )
    assert child_resp.status_code == 201, child_resp.text
    assert child_resp.json()["parent_id"] is not None
    child_lock_version = child_resp.json()["lock_version"]

    patch_resp = await authed_client.patch(
        f"/api/v1/projects/{project.key}/wiki/nested-child/",
        json={"text": "child", "lock_version": child_lock_version, "parent_slug": ""},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json()["parent_id"] is None


@pytest.mark.asyncio
async def test_update_wiki_page_rename_and_reparent(
    authed_client: AsyncClient,
    project: Project,
):
    """PATCH with title + parent_slug + text all update simultaneously."""
    parent_resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Multi Parent", "text": "parent"},
    )
    assert parent_resp.status_code == 201, parent_resp.text
    parent_id = parent_resp.json()["id"]

    child_resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Multi Child Old", "text": "old child text"},
    )
    assert child_resp.status_code == 201, child_resp.text
    child_lock_version = child_resp.json()["lock_version"]
    assert child_resp.json()["parent_id"] is None

    patch_resp = await authed_client.patch(
        f"/api/v1/projects/{project.key}/wiki/multi-child-old/",
        json={
            "title": "Multi Child New",
            "text": "new child text",
            "lock_version": child_lock_version,
            "parent_slug": "multi-parent",
        },
    )
    assert patch_resp.status_code == 200, patch_resp.text
    data = patch_resp.json()
    assert data["title"] == "Multi Child New"
    assert data["slug"] == "multi-child-new"
    assert data["text"] == "new child text"
    assert data["parent_id"] == parent_id


@pytest.mark.asyncio
async def test_update_wiki_page_invalid_parent_slug(
    authed_client: AsyncClient,
    project: Project,
):
    """PATCH with a nonexistent parent_slug returns 404."""
    create_resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Orphan Page", "text": "content"},
    )
    assert create_resp.status_code == 201, create_resp.text
    lock_version = create_resp.json()["lock_version"]

    patch_resp = await authed_client.patch(
        f"/api/v1/projects/{project.key}/wiki/orphan-page/",
        json={
            "text": "content",
            "lock_version": lock_version,
            "parent_slug": "nonexistent-parent",
        },
    )
    assert patch_resp.status_code == 404
