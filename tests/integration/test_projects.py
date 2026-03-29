"""Integration tests for Projects API.

Tests cover:
- Project CRUD (create, read, update, delete)
- Child project creation (ltree path)
- Pagination list
- Member add / remove / list
- Module toggle
- Permission enforcement (non-member cannot edit private project)
- Unique constraint violations (409)
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.role import Role
from specivo.models.user import User
from tests.factories.user import AdminUserFactory, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_role(db_session: AsyncSession, name: str = "Manager") -> Role:
    role = Role(
        name=name,
        permissions=["*"],
        builtin=0,
        assignable=True,
    )
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


async def _make_user(db_session: AsyncSession, **kwargs) -> User:
    user = UserFactory.build(**kwargs)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_admin(db_session: AsyncSession, **kwargs) -> User:
    user = AdminUserFactory.build(**kwargs)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _login(client: AsyncClient, login: str, password: str = "testpassword") -> str:
    resp = await client.post("/api/v1/auth/login", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_token(db_session: AsyncSession, client: AsyncClient) -> str:
    user = await _make_admin(db_session, login="admin_proj_test", status="active")
    return await _login(client, user.login)


@pytest_asyncio.fixture
async def user_token(db_session: AsyncSession, client: AsyncClient) -> str:
    user = await _make_user(db_session, login="regular_proj_user", status="active")
    return await _login(client, user.login)


@pytest_asyncio.fixture
async def regular_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="regular_user_b", status="active")


# ---------------------------------------------------------------------------
# Tests: Project CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_project(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
) -> None:
    resp = await client.post(
        "/api/v1/projects",
        json={
            "name": "Specivo Tracker",
            "identifier": "specivo-tracker",
            "key": "SPV",
            "description": "Main project",
            "is_public": True,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["key"] == "SPV"
    assert data["identifier"] == "specivo-tracker"
    assert data["name"] == "Specivo Tracker"
    assert data["path"] == "specivo_tracker"
    assert data["parent_id"] is None
    assert data["parent_key"] is None
    assert data["status"] == 1


@pytest.mark.asyncio
async def test_create_child_project(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
) -> None:
    # Create parent
    await client.post(
        "/api/v1/projects",
        json={"name": "Parent", "identifier": "parent-proj", "key": "PAR", "is_public": True},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    # Create child
    resp = await client.post(
        "/api/v1/projects",
        json={
            "name": "Child Project",
            "identifier": "child-proj",
            "key": "CHD",
            "parent_key": "PAR",
            "is_public": True,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["key"] == "CHD"
    assert data["parent_key"] == "PAR"
    assert data["path"] == "parent_proj.child_proj"


@pytest.mark.asyncio
async def test_get_project(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
) -> None:
    await client.post(
        "/api/v1/projects",
        json={"name": "Get Test", "identifier": "get-test", "key": "GET"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    resp = await client.get(
        "/api/v1/projects/GET",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["key"] == "GET"


@pytest.mark.asyncio
async def test_get_project_not_found(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
) -> None:
    resp = await client.get(
        "/api/v1/projects/NOTEXIST",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_project(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
) -> None:
    await client.post(
        "/api/v1/projects",
        json={"name": "Update Me", "identifier": "update-me", "key": "UPD"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    resp = await client.patch(
        "/api/v1/projects/UPD",
        json={"name": "Updated Name", "status": 5},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "Updated Name"
    assert data["status"] == 5


@pytest.mark.asyncio
async def test_delete_project(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
) -> None:
    await client.post(
        "/api/v1/projects",
        json={"name": "Delete Me", "identifier": "delete-me", "key": "DEL"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    resp = await client.delete(
        "/api/v1/projects/DEL",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 204

    get_resp = await client.get(
        "/api/v1/projects/DEL",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_list_projects(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
) -> None:
    for i in range(3):
        await client.post(
            "/api/v1/projects",
            json={
                "name": f"List Project {i}",
                "identifier": f"list-project-{i}",
                "key": f"LP{i}",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    resp = await client.get(
        "/api/v1/projects",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "items" in data
    assert data["total_count"] >= 3


# ---------------------------------------------------------------------------
# Tests: Unique constraint → 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_key_returns_409(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
) -> None:
    await client.post(
        "/api/v1/projects",
        json={"name": "Dup Key", "identifier": "dup-key-1", "key": "DUP"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    resp = await client.post(
        "/api/v1/projects",
        json={"name": "Dup Key 2", "identifier": "dup-key-2", "key": "DUP"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_duplicate_identifier_returns_409(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
) -> None:
    await client.post(
        "/api/v1/projects",
        json={"name": "Dup ID", "identifier": "dup-identifier", "key": "DI1"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    resp = await client.post(
        "/api/v1/projects",
        json={"name": "Dup ID 2", "identifier": "dup-identifier", "key": "DI2"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------------
# Tests: Members
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_member(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    regular_user: User,
) -> None:
    role = await _make_role(db_session, "Developer")

    await client.post(
        "/api/v1/projects",
        json={"name": "Member Test", "identifier": "member-test", "key": "MBT"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    resp = await client.post(
        "/api/v1/projects/MBT/members",
        json={"user_id": regular_user.id, "role_ids": [role.id]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["user_id"] == regular_user.id
    assert "Developer" in data["roles"]


@pytest.mark.asyncio
async def test_list_members(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    regular_user: User,
) -> None:
    role = await _make_role(db_session, "Viewer")

    await client.post(
        "/api/v1/projects",
        json={"name": "List Members", "identifier": "list-members", "key": "LMB"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    await client.post(
        "/api/v1/projects/LMB/members",
        json={"user_id": regular_user.id, "role_ids": [role.id]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    resp = await client.get(
        "/api/v1/projects/LMB/members",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    members = resp.json()
    user_ids = [m["user_id"] for m in members]
    assert regular_user.id in user_ids


@pytest.mark.asyncio
async def test_remove_member(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    regular_user: User,
) -> None:
    role = await _make_role(db_session, "Contributor")

    await client.post(
        "/api/v1/projects",
        json={"name": "Remove Member", "identifier": "remove-member", "key": "RMB"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    await client.post(
        "/api/v1/projects/RMB/members",
        json={"user_id": regular_user.id, "role_ids": [role.id]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    resp = await client.delete(
        f"/api/v1/projects/RMB/members/{regular_user.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 204

    list_resp = await client.get(
        "/api/v1/projects/RMB/members",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    user_ids = [m["user_id"] for m in list_resp.json()]
    assert regular_user.id not in user_ids


# ---------------------------------------------------------------------------
# Tests: Modules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_modules_default(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
) -> None:
    await client.post(
        "/api/v1/projects",
        json={"name": "Module Test", "identifier": "module-test", "key": "MOD"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    resp = await client.get(
        "/api/v1/projects/MOD/modules",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    modules = resp.json()["modules"]
    # issue_tracking is enabled by default
    assert modules.get("issue_tracking") is True


@pytest.mark.asyncio
async def test_toggle_modules(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
) -> None:
    await client.post(
        "/api/v1/projects",
        json={"name": "Toggle Modules", "identifier": "toggle-modules", "key": "TGM"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    resp = await client.patch(
        "/api/v1/projects/TGM/modules",
        json={"modules": {"wiki": True, "time_tracking": True}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    modules = resp.json()["modules"]
    assert modules["wiki"] is True
    assert modules["time_tracking"] is True
    assert modules["issue_tracking"] is True  # still enabled

    # Now disable one
    resp2 = await client.patch(
        "/api/v1/projects/TGM/modules",
        json={"modules": {"wiki": False}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["modules"]["wiki"] is False


# ---------------------------------------------------------------------------
# Tests: Permission enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_admin_cannot_create_project(
    client: AsyncClient,
    db_session: AsyncSession,
    user_token: str,
) -> None:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": "No Perms", "identifier": "no-perms", "key": "NOP"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_non_member_cannot_access_private_project(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    user_token: str,
) -> None:
    await client.post(
        "/api/v1/projects",
        json={
            "name": "Private Project",
            "identifier": "private-project",
            "key": "PRV",
            "is_public": False,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    resp = await client.get(
        "/api/v1/projects/PRV",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert resp.status_code == 404  # 404 not 403 — prevents enumeration


@pytest.mark.asyncio
async def test_non_member_cannot_edit_project(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    user_token: str,
) -> None:
    await client.post(
        "/api/v1/projects",
        json={"name": "Edit Guard", "identifier": "edit-guard", "key": "EDG"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    resp = await client.patch(
        "/api/v1/projects/EDG",
        json={"name": "Hacked Name"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_member_with_manage_project_can_edit(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    db_engine,
) -> None:
    """A non-admin user with 'manage_project' permission can edit the project."""
    # Create a manager role with manage_project permission
    # Use db_session (inside the rollback transaction) — NOT a separate session
    role = Role(name="ProjectManager", permissions=["manage_project"], builtin=0)
    user = UserFactory.build(login="pm_user", status="active")
    db_session.add(role)
    db_session.add(user)
    await db_session.flush()
    pm_user_id = user.id
    pm_role_id = role.id

    pm_token = await _login(client, "pm_user")

    # Admin creates project
    await client.post(
        "/api/v1/projects",
        json={"name": "PM Project", "identifier": "pm-project", "key": "PMJ"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    # Admin adds pm_user as member with ProjectManager role
    await client.post(
        "/api/v1/projects/PMJ/members",
        json={"user_id": pm_user_id, "role_ids": [pm_role_id]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    # pm_user can now edit
    resp = await client.patch(
        "/api/v1/projects/PMJ",
        json={"name": "PM Updated Name"},
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "PM Updated Name"


@pytest.mark.asyncio
async def test_public_project_visible_to_non_member(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    user_token: str,
) -> None:
    await client.post(
        "/api/v1/projects",
        json={"name": "Public Proj", "identifier": "public-proj", "key": "PUB"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    resp = await client.get(
        "/api/v1/projects/PUB",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert resp.status_code == 200, resp.text
