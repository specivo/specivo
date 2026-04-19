"""Integration tests for the Sprint API endpoints.

These tests are written TDD-first and will fail until the feature is
implemented. They drive the expected HTTP interface.

Covered:
API endpoints:
- POST   /api/v1/projects/{key}/sprints/              — create sprint (201)
- POST   without manage_sprints permission             — 403
- GET    /api/v1/projects/{key}/sprints/               — list sprints
- GET    /api/v1/projects/{key}/sprints/{id}/          — get sprint by ID
- PATCH  /api/v1/projects/{key}/sprints/{id}/          — update sprint
- DELETE /api/v1/projects/{key}/sprints/{id}/          — delete sprint (204)
- POST   /api/v1/projects/{key}/sprints/{id}/start/    — start sprint (200)
- POST   start when another is active                  — 409
- POST   /api/v1/projects/{key}/sprints/{id}/complete/ — complete sprint (200)
- GET    /api/v1/projects/{key}/sprints/{id}/board/    — board columns
- GET    /api/v1/projects/{key}/backlog/               — unsprinted issues
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession, login: str = "sprint_api_user") -> User:
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


async def _make_project(
    db: AsyncSession,
    key: str = "SAPI",
    identifier: str = "sprint-api",
) -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


async def _enable_issue_tracking(db: AsyncSession, project: Project) -> None:
    db.add(EnabledModule(project_id=project.id, name="issue_tracking"))
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


async def _make_lookups(
    db: AsyncSession,
) -> tuple[Tracker, IssueStatus, IssueStatus, IssuePriority]:
    """Create tracker, open status, closed status, and priority."""
    status_open = StatusFactory.build(name="New", position=1, category="backlog")
    status_done = StatusFactory.build(name="Done", position=5, category="done")
    priority = PriorityFactory.build(name="Normal", is_default=True, position=1)

    db.add_all([status_open, status_done, priority])
    await db.commit()
    await db.refresh(status_open)
    await db.refresh(status_done)
    await db.refresh(priority)

    tracker = TrackerFactory.build(name="Task", default_status_id=status_open.id)
    db.add(tracker)
    await db.commit()
    await db.refresh(tracker)

    return tracker, status_open, status_done, priority


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = await _make_project(db_session)
    await _enable_issue_tracking(db_session, proj)
    return proj


@pytest_asyncio.fixture
async def sprint_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="sprint_api_mgr")


@pytest_asyncio.fixture
async def authed_client(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
    sprint_user: User,
) -> AsyncClient:
    """Client authenticated as a manager with manage_sprints permission."""
    await _add_member_with_permissions(
        db_session,
        project,
        sprint_user,
        ["view_issues", "add_issues", "manage_sprints"],
    )
    token = await _login(client, sprint_user.login)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest_asyncio.fixture
async def versions_only_client(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
) -> AsyncClient:
    """Client with manage_versions but NOT manage_sprints.

    Used to verify that the legacy permission no longer authorizes sprint
    management operations after the permission split.
    """
    user = await _make_user(db_session, login="sprint_api_versions_only")
    await _add_member_with_permissions(
        db_session, project, user, ["view_issues", "add_issues", "manage_versions"]
    )
    token = await _login(client, user.login)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest_asyncio.fixture
async def viewer_client(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
) -> AsyncClient:
    """Client authenticated as a view-only member (no manage_sprints)."""
    viewer = await _make_user(db_session, login="sprint_api_viewer")
    await _add_member_with_permissions(
        db_session, project, viewer, ["view_issues"]
    )
    token = await _login(client, viewer.login)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest_asyncio.fixture
async def lookups(
    db_session: AsyncSession,
) -> tuple[Tracker, IssueStatus, IssueStatus, IssuePriority]:
    return await _make_lookups(db_session)


# ---------------------------------------------------------------------------
# Helper: create sprint and issue via API
# ---------------------------------------------------------------------------


async def _api_create_sprint(
    client: AsyncClient,
    project_key: str,
    name: str = "Sprint 1",
    **kwargs,
) -> dict:
    payload = {"name": name, **kwargs}
    resp = await client.post(
        f"/api/v1/projects/{project_key}/sprints/",
        json=payload,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _api_create_issue(
    client: AsyncClient,
    project_key: str,
    tracker_id: int,
    status_id: int,
    priority_id: int,
    subject: str,
    *,
    sprint_id: int | None = None,
) -> dict:
    payload: dict = {
        "project_key": project_key,
        "tracker_id": tracker_id,
        "subject": subject,
        "status_id": status_id,
        "priority_id": priority_id,
    }
    if sprint_id is not None:
        payload["sprint_id"] = sprint_id

    resp = await client.post(
        f"/api/v1/projects/{project_key}/issues/",
        json=payload,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Tests: Sprint CRUD
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_api_create_sprint_201(
    authed_client: AsyncClient,
    project: Project,
):
    resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/sprints/",
        json={
            "name": "Sprint 1",
            "goal": "Complete onboarding flow",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["name"] == "Sprint 1"
    assert data["status"] == "planned"
    assert data["goal"] == "Complete onboarding flow"
    assert data["project_id"] == project.id
    assert "id" in data


@pytest.mark.integration
async def test_api_create_sprint_requires_permission(
    viewer_client: AsyncClient,
    project: Project,
):
    resp = await viewer_client.post(
        f"/api/v1/projects/{project.key}/sprints/",
        json={"name": "Unauthorized Sprint"},
    )
    assert resp.status_code == 403


@pytest.mark.integration
async def test_api_list_sprints(
    authed_client: AsyncClient,
    project: Project,
):
    await _api_create_sprint(authed_client, project.key, "Sprint A")
    await _api_create_sprint(authed_client, project.key, "Sprint B")

    resp = await authed_client.get(
        f"/api/v1/projects/{project.key}/sprints/",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2


@pytest.mark.integration
async def test_api_get_sprint(
    authed_client: AsyncClient,
    project: Project,
):
    sprint = await _api_create_sprint(authed_client, project.key, "Get Me")

    resp = await authed_client.get(
        f"/api/v1/projects/{project.key}/sprints/{sprint['id']}/",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == sprint["id"]
    assert data["name"] == "Get Me"


@pytest.mark.integration
async def test_api_update_sprint(
    authed_client: AsyncClient,
    project: Project,
):
    sprint = await _api_create_sprint(authed_client, project.key, "Old Name")

    resp = await authed_client.patch(
        f"/api/v1/projects/{project.key}/sprints/{sprint['id']}/",
        json={"name": "New Name", "goal": "Updated goal"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "New Name"
    assert data["goal"] == "Updated goal"


@pytest.mark.integration
async def test_api_delete_sprint_204(
    authed_client: AsyncClient,
    project: Project,
):
    sprint = await _api_create_sprint(authed_client, project.key, "Delete Me")

    resp = await authed_client.delete(
        f"/api/v1/projects/{project.key}/sprints/{sprint['id']}/",
    )
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Tests: Sprint lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_api_start_sprint(
    authed_client: AsyncClient,
    project: Project,
):
    sprint = await _api_create_sprint(authed_client, project.key, "Start Me")

    resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/sprints/{sprint['id']}/start/",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "active"


@pytest.mark.integration
async def test_api_start_second_sprint_409(
    authed_client: AsyncClient,
    project: Project,
):
    sprint1 = await _api_create_sprint(authed_client, project.key, "First Active")
    await authed_client.post(
        f"/api/v1/projects/{project.key}/sprints/{sprint1['id']}/start/",
    )

    sprint2 = await _api_create_sprint(authed_client, project.key, "Second Sprint")
    resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/sprints/{sprint2['id']}/start/",
    )
    assert resp.status_code == 409


@pytest.mark.integration
async def test_api_complete_sprint(
    authed_client: AsyncClient,
    project: Project,
):
    sprint = await _api_create_sprint(authed_client, project.key, "Complete Me")
    await authed_client.post(
        f"/api/v1/projects/{project.key}/sprints/{sprint['id']}/start/",
    )

    resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/sprints/{sprint['id']}/complete/",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "completed"


# ---------------------------------------------------------------------------
# Tests: Board and backlog
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_api_board(
    authed_client: AsyncClient,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssueStatus, IssuePriority],
):
    tracker, status_open, status_done, priority = lookups
    sprint = await _api_create_sprint(authed_client, project.key, "Board Sprint")
    await authed_client.post(
        f"/api/v1/projects/{project.key}/sprints/{sprint['id']}/start/",
    )

    # Create issues assigned to the sprint
    await _api_create_issue(
        authed_client, project.key,
        tracker.id, status_open.id, priority.id,
        "Board Task 1",
        sprint_id=sprint["id"],
    )
    await _api_create_issue(
        authed_client, project.key,
        tracker.id, status_done.id, priority.id,
        "Board Task 2",
        sprint_id=sprint["id"],
    )

    resp = await authed_client.get(
        f"/api/v1/projects/{project.key}/sprints/{sprint['id']}/board/",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # Board returns columns (dict of status_name -> issues)
    assert isinstance(data, dict)
    total_issues = sum(len(issues) for issues in data.values())
    assert total_issues == 2


@pytest.mark.integration
async def test_api_backlog(
    authed_client: AsyncClient,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssueStatus, IssuePriority],
):
    tracker, status_open, status_done, priority = lookups
    sprint = await _api_create_sprint(authed_client, project.key, "Some Sprint")

    # Create one sprinted and one unsprinted issue
    await _api_create_issue(
        authed_client, project.key,
        tracker.id, status_open.id, priority.id,
        "Sprinted Task",
        sprint_id=sprint["id"],
    )
    await _api_create_issue(
        authed_client, project.key,
        tracker.id, status_open.id, priority.id,
        "Backlog Task",
    )

    resp = await authed_client.get(
        f"/api/v1/projects/{project.key}/backlog/",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list)
    subjects = [i["subject"] for i in data]
    assert "Backlog Task" in subjects
    assert "Sprinted Task" not in subjects


# ---------------------------------------------------------------------------
# Tests: manage_sprints / manage_versions permission split
# ---------------------------------------------------------------------------


async def _seed_sprint_directly(
    db: AsyncSession,
    project: Project,
    name: str,
    *,
    start: bool = False,
) -> int:
    """Create a sprint directly via the service, bypassing API auth.

    Used by permission-split tests so a single ``client`` instance can swap
    its bearer token between users without fixture-ordering surprises.
    """
    from specivo.schemas.sprint import SprintCreate
    from specivo.services.sprint_service import SprintService

    svc = SprintService()
    sprint = await svc.create(db, project, SprintCreate(name=name))
    if start:
        sprint = await svc.start_sprint(db, sprint)
    await db.commit()
    return sprint.id


@pytest.mark.integration
async def test_manage_versions_alone_cannot_create_sprint(
    versions_only_client: AsyncClient,
    project: Project,
):
    """A user with manage_versions but NOT manage_sprints is denied sprint create."""
    resp = await versions_only_client.post(
        f"/api/v1/projects/{project.key}/sprints/",
        json={"name": "Should Fail"},
    )
    assert resp.status_code == 403


@pytest.mark.integration
async def test_manage_versions_alone_cannot_update_sprint(
    db_session: AsyncSession,
    versions_only_client: AsyncClient,
    project: Project,
):
    """manage_versions does not authorize sprint updates."""
    sprint_id = await _seed_sprint_directly(db_session, project, "Owned")
    resp = await versions_only_client.patch(
        f"/api/v1/projects/{project.key}/sprints/{sprint_id}/",
        json={"name": "Hijacked"},
    )
    assert resp.status_code == 403


@pytest.mark.integration
async def test_manage_versions_alone_cannot_start_sprint(
    db_session: AsyncSession,
    versions_only_client: AsyncClient,
    project: Project,
):
    sprint_id = await _seed_sprint_directly(db_session, project, "Locked")
    resp = await versions_only_client.post(
        f"/api/v1/projects/{project.key}/sprints/{sprint_id}/start/",
    )
    assert resp.status_code == 403


@pytest.mark.integration
async def test_manage_versions_alone_cannot_complete_sprint(
    db_session: AsyncSession,
    versions_only_client: AsyncClient,
    project: Project,
):
    sprint_id = await _seed_sprint_directly(db_session, project, "Running", start=True)
    resp = await versions_only_client.post(
        f"/api/v1/projects/{project.key}/sprints/{sprint_id}/complete/",
    )
    assert resp.status_code == 403


@pytest.mark.integration
async def test_manage_versions_alone_cannot_delete_sprint(
    db_session: AsyncSession,
    versions_only_client: AsyncClient,
    project: Project,
):
    sprint_id = await _seed_sprint_directly(db_session, project, "Doomed")
    resp = await versions_only_client.delete(
        f"/api/v1/projects/{project.key}/sprints/{sprint_id}/",
    )
    assert resp.status_code == 403


@pytest.mark.integration
async def test_manage_sprints_alone_can_run_full_lifecycle(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
):
    """A user with manage_sprints but NOT manage_versions can do all sprint ops."""
    user = await _make_user(db_session, login="sprint_api_sprints_only")
    await _add_member_with_permissions(
        db_session, project, user, ["view_issues", "manage_sprints"]
    )
    token = await _login(client, user.login)
    client.headers["Authorization"] = f"Bearer {token}"

    # Create
    create_resp = await client.post(
        f"/api/v1/projects/{project.key}/sprints/",
        json={"name": "Lifecycle"},
    )
    assert create_resp.status_code == 201, create_resp.text
    sprint_id = create_resp.json()["id"]

    # Update
    upd = await client.patch(
        f"/api/v1/projects/{project.key}/sprints/{sprint_id}/",
        json={"goal": "ship it"},
    )
    assert upd.status_code == 200, upd.text

    # Start
    start = await client.post(
        f"/api/v1/projects/{project.key}/sprints/{sprint_id}/start/",
    )
    assert start.status_code == 200, start.text

    # Complete
    done = await client.post(
        f"/api/v1/projects/{project.key}/sprints/{sprint_id}/complete/",
    )
    assert done.status_code == 200, done.text

    # Delete
    delete = await client.delete(
        f"/api/v1/projects/{project.key}/sprints/{sprint_id}/",
    )
    assert delete.status_code == 204


# ---------------------------------------------------------------------------
# Tests: Sprint search endpoint (autocomplete)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_api_search_sprints_empty_returns_recent_10(
    authed_client: AsyncClient,
    project: Project,
) -> None:
    """Empty query returns up to 10 most recent sprints, newest first by start_date."""
    import datetime as _dt

    for i in range(12):
        await authed_client.post(
            f"/api/v1/projects/{project.key}/sprints/",
            json={
                "name": f"Sprint {i:02d}",
                "start_date": (_dt.date(2026, 1, 1) + _dt.timedelta(days=i)).isoformat(),
                "end_date": (_dt.date(2026, 1, 15) + _dt.timedelta(days=i)).isoformat(),
            },
        )

    resp = await authed_client.get(
        f"/api/v1/projects/{project.key}/sprints/search/",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 10
    assert data[0]["name"] == "Sprint 11"
    assert data[-1]["name"] == "Sprint 02"
    assert {"id", "name", "status", "start_date", "end_date"} <= set(data[0].keys())


@pytest.mark.integration
async def test_api_search_sprints_substring_match(
    authed_client: AsyncClient,
    project: Project,
) -> None:
    """Non-empty query performs case-insensitive substring match on name."""
    for name in ["Alpha one", "alpha two", "Beta one"]:
        await authed_client.post(
            f"/api/v1/projects/{project.key}/sprints/",
            json={"name": name},
        )

    resp = await authed_client.get(
        f"/api/v1/projects/{project.key}/sprints/search/?q=ALPHA",
    )
    assert resp.status_code == 200, resp.text
    names = {s["name"] for s in resp.json()}
    assert names == {"Alpha one", "alpha two"}


@pytest.mark.integration
async def test_api_search_sprints_viewer_allowed(
    viewer_client: AsyncClient,
    project: Project,
) -> None:
    """A view_issues-only member can search sprints."""
    resp = await viewer_client.get(
        f"/api/v1/projects/{project.key}/sprints/search/",
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


@pytest.mark.integration
async def test_api_search_sprints_requires_auth(
    client: AsyncClient,
    project: Project,
) -> None:
    resp = await client.get(f"/api/v1/projects/{project.key}/sprints/search/")
    assert resp.status_code == 401
