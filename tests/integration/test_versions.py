"""Integration tests for the Versions API and roadmap.

Covers:
- Version CRUD (create, list, get, update, delete)
- list ordered by effective_date
- Roadmap: open/closed counts per version
- Roadmap: progress_percent calculation
- Version sharing: system-shared version visible across projects
- Permission enforcement: non-manager cannot manage versions
"""

from __future__ import annotations

import datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from specivo.models.version import Version
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory
from tests.factories.version import VersionFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_admin(db: AsyncSession, login: str = "admin_ver_test") -> User:
    user = AdminUserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_user(db: AsyncSession, login: str = "user_ver_test") -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client: AsyncClient, login: str, password: str = "testpassword") -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _make_project(db: AsyncSession, key: str, identifier: str) -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


async def _make_version(
    db: AsyncSession,
    project: Project,
    name: str = "v1.0",
    **kwargs,
) -> Version:
    version = VersionFactory.build(project_id=project.id, name=name, **kwargs)
    db.add(version)
    await db.commit()
    await db.refresh(version)
    return version


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_token(db_session: AsyncSession, client: AsyncClient) -> str:
    user = await _make_admin(db_session)
    return await _login(client, user.login)


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    return await _make_project(db_session, key="VER", identifier="ver-project")


@pytest_asyncio.fixture
async def status_open(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def status_closed(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="Closed", position=5, category="closed")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, status_open: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Bug", default_status_id=status_open.id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def priority(db_session: AsyncSession) -> IssuePriority:
    p = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def author(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="issue_author_ver", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Tests: Version CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_version(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
) -> None:
    resp = await client.post(
        f"/api/v1/projects/{project.key}/versions/",
        json={
            "name": "v1.0",
            "description": "First release",
            "status": "open",
            "effective_date": "2026-05-31",
            "sharing": "none",
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["name"] == "v1.0"
    assert data["description"] == "First release"
    assert data["status"] == "open"
    assert data["effective_date"] == "2026-05-31"
    assert data["sharing"] == "none"
    assert data["project_key"] == project.key
    assert "id" in data
    assert "created_at" in data


@pytest.mark.asyncio
async def test_list_versions_empty(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
) -> None:
    resp = await client.get(
        f"/api/v1/projects/{project.key}/versions/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_versions_ordered_by_effective_date(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
) -> None:
    """Versions ordered by effective_date ASC nulls last, then name."""
    # Insert in reverse date order
    await _make_version(
        db_session,
        project,
        name="v3.0",
        effective_date=datetime.date(2027, 11, 28),
    )
    await _make_version(
        db_session,
        project,
        name="v1.0",
        effective_date=datetime.date(2026, 3, 1),
    )
    await _make_version(
        db_session,
        project,
        name="v2.0",
        effective_date=datetime.date(2026, 8, 30),
    )
    await _make_version(
        db_session,
        project,
        name="vZ-no-date",
        effective_date=None,
    )
    await _make_version(
        db_session,
        project,
        name="vA-no-date",
        effective_date=None,
    )

    resp = await client.get(
        f"/api/v1/projects/{project.key}/versions/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    names = [item["name"] for item in resp.json()]

    # Dated first, sorted by date ASC, then undated sorted by name ASC
    assert names.index("v1.0") < names.index("v2.0")
    assert names.index("v2.0") < names.index("v3.0")
    # Null-date entries come last
    assert names.index("v3.0") < names.index("vA-no-date")
    assert names.index("vA-no-date") < names.index("vZ-no-date")


@pytest.mark.asyncio
async def test_get_version(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
) -> None:
    version = await _make_version(db_session, project, name="v2.0")

    resp = await client.get(
        f"/api/v1/projects/{project.key}/versions/{version.id}/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == version.id
    assert data["name"] == "v2.0"
    assert data["project_key"] == project.key


@pytest.mark.asyncio
async def test_get_version_not_found(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
) -> None:
    resp = await client.get(
        f"/api/v1/projects/{project.key}/versions/999999/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_update_version(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
) -> None:
    version = await _make_version(db_session, project, name="v1.0-alpha")

    resp = await client.patch(
        f"/api/v1/projects/{project.key}/versions/{version.id}/",
        json={
            "name": "v1.0",
            "status": "locked",
            "effective_date": "2026-07-12",
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "v1.0"
    assert data["status"] == "locked"
    assert data["effective_date"] == "2026-07-12"


@pytest.mark.asyncio
async def test_delete_version(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
) -> None:
    version = await _make_version(db_session, project, name="to-delete")

    resp = await client.delete(
        f"/api/v1/projects/{project.key}/versions/{version.id}/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 204, resp.text

    get_resp = await client.get(
        f"/api/v1/projects/{project.key}/versions/{version.id}/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_create_version_requires_auth(
    client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
) -> None:
    resp = await client.post(
        f"/api/v1/projects/{project.key}/versions/",
        json={"name": "no-auth"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_version_permission_denied_for_non_member(
    client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
) -> None:
    """A regular user with no role on the project cannot manage versions.

    Returns 404 (not 403) because require_project_access runs first and
    returns 404 for non-members on private projects to prevent enumeration.
    """
    regular = await _make_user(db_session, login="regular_ver_user")
    token = await _login(client, regular.login)

    resp = await client.post(
        f"/api/v1/projects/{project.key}/versions/",
        json={"name": "blocked"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Tests: Roadmap
# ---------------------------------------------------------------------------


async def _seed_issue(
    db: AsyncSession,
    project: Project,
    version: Version,
    status: IssueStatus,
    tracker: Tracker,
    priority: IssuePriority,
    author: User,
    seq: int,
) -> Issue:
    issue = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=seq,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=author.id,
        subject=f"Issue {seq}",
        fixed_version_id=version.id,
        lft=1,
        rgt=2,
        lock_version=0,
    )
    db.add(issue)
    await db.commit()
    await db.refresh(issue)
    return issue


@pytest.mark.asyncio
async def test_roadmap_empty(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
) -> None:
    resp = await client.get(
        f"/api/v1/projects/{project.key}/roadmap/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


@pytest.mark.asyncio
async def test_roadmap_open_closed_counts(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    status_open: IssueStatus,
    status_closed: IssueStatus,
    tracker: Tracker,
    priority: IssuePriority,
    author: User,
) -> None:
    """Roadmap returns correct open/closed counts per version."""
    version = await _make_version(db_session, project, name="v1.0")

    # 3 open, 2 closed
    seq = 1
    for _ in range(3):
        await _seed_issue(db_session, project, version, status_open, tracker, priority, author, seq)
        seq += 1
    for _ in range(2):
        await _seed_issue(db_session, project, version, status_closed, tracker, priority, author, seq)
        seq += 1

    resp = await client.get(
        f"/api/v1/projects/{project.key}/roadmap/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    entries = resp.json()
    assert len(entries) == 1

    entry = entries[0]
    assert entry["version"]["name"] == "v1.0"
    assert entry["open_count"] == 3
    assert entry["closed_count"] == 2
    assert entry["total"] == 5


@pytest.mark.asyncio
async def test_roadmap_progress_percent(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    status_open: IssueStatus,
    status_closed: IssueStatus,
    tracker: Tracker,
    priority: IssuePriority,
    author: User,
) -> None:
    """progress_percent = closed / total * 100, integer."""
    version = await _make_version(db_session, project, name="v2.0")

    # 1 closed out of 4 total → 25%
    seq = 100
    for _ in range(3):
        await _seed_issue(db_session, project, version, status_open, tracker, priority, author, seq)
        seq += 1
    await _seed_issue(db_session, project, version, status_closed, tracker, priority, author, seq)

    resp = await client.get(
        f"/api/v1/projects/{project.key}/roadmap/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    entries = resp.json()
    assert len(entries) == 1
    assert entries[0]["progress_percent"] == 25


@pytest.mark.asyncio
async def test_roadmap_progress_zero_when_no_issues(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
) -> None:
    """Version with no issues has progress_percent=0 and all counts=0."""
    await _make_version(db_session, project, name="empty-version")

    resp = await client.get(
        f"/api/v1/projects/{project.key}/roadmap/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    entries = resp.json()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["open_count"] == 0
    assert entry["closed_count"] == 0
    assert entry["total"] == 0
    assert entry["progress_percent"] == 0


@pytest.mark.asyncio
async def test_roadmap_multiple_versions(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    status_open: IssueStatus,
    status_closed: IssueStatus,
    tracker: Tracker,
    priority: IssuePriority,
    author: User,
) -> None:
    """Roadmap lists all versions for the project in order."""
    v1 = await _make_version(
        db_session,
        project,
        name="v1.0",
        effective_date=datetime.date(2026, 5, 31),
    )
    v2 = await _make_version(
        db_session,
        project,
        name="v2.0",
        effective_date=datetime.date(2026, 11, 29),
    )

    await _seed_issue(db_session, project, v1, status_closed, tracker, priority, author, 200)
    await _seed_issue(db_session, project, v2, status_open, tracker, priority, author, 201)

    resp = await client.get(
        f"/api/v1/projects/{project.key}/roadmap/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    entries = resp.json()
    assert len(entries) == 2

    names = [e["version"]["name"] for e in entries]
    assert names[0] == "v1.0"
    assert names[1] == "v2.0"

    e1 = next(e for e in entries if e["version"]["name"] == "v1.0")
    e2 = next(e for e in entries if e["version"]["name"] == "v2.0")

    assert e1["closed_count"] == 1
    assert e1["open_count"] == 0
    assert e1["progress_percent"] == 100

    assert e2["open_count"] == 1
    assert e2["closed_count"] == 0
    assert e2["progress_percent"] == 0


# ---------------------------------------------------------------------------
# Tests: Sharing — system-shared version visible across projects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_shared_version_visible_to_other_project(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
) -> None:
    """A version with sharing='system' appears in visible_versions for any project."""
    from specivo.schemas.version import VersionCreate
    from specivo.services.version_service import VersionService

    proj_a = await _make_project(db_session, key="SHA", identifier="sharing-a")
    proj_b = await _make_project(db_session, key="SHB", identifier="sharing-b")

    # Create a system-shared version in project A
    version_create = VersionCreate(
        name="global-release",
        sharing="system",
    )

    svc = VersionService()
    version = await svc.create(db_session, proj_a, version_create)
    await db_session.commit()
    await db_session.refresh(version)

    # Verify it's visible from project B
    visible = await svc.visible_versions(db_session, proj_b)
    visible_ids = [v.id for v in visible]
    assert version.id in visible_ids, f"system-shared version {version.id} should be visible from proj_b"


@pytest.mark.asyncio
async def test_none_sharing_not_visible_to_other_project(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
) -> None:
    """A version with sharing='none' is NOT visible from another project."""
    from specivo.schemas.version import VersionCreate
    from specivo.services.version_service import VersionService

    proj_a = await _make_project(db_session, key="NSA", identifier="none-sharing-a")
    proj_b = await _make_project(db_session, key="NSB", identifier="none-sharing-b")

    svc = VersionService()
    version = await svc.create(
        db_session,
        proj_a,
        VersionCreate(name="private-release", sharing="none"),
    )
    await db_session.commit()
    await db_session.refresh(version)

    visible = await svc.visible_versions(db_session, proj_b)
    visible_ids = [v.id for v in visible]
    assert version.id not in visible_ids, f"none-shared version {version.id} should NOT be visible from proj_b"


@pytest.mark.asyncio
async def test_descendants_sharing_visible_to_subproject(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
) -> None:
    """A version with sharing='descendants' is visible to subprojects."""
    from specivo.schemas.version import VersionCreate
    from specivo.services.version_service import VersionService

    # Parent project
    parent = ProjectFactory.build(key="DSP", identifier="desc-parent", path="desc_parent")
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    # Child project (subproject)
    child = ProjectFactory.build(
        key="DSC",
        identifier="desc-child",
        path="desc_parent.desc_child",
        parent_id=parent.id,
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)

    svc = VersionService()
    version = await svc.create(
        db_session,
        parent,
        VersionCreate(name="parent-release", sharing="descendants"),
    )
    await db_session.commit()
    await db_session.refresh(version)

    # Child project should see the parent's version
    visible = await svc.visible_versions(db_session, child)
    visible_ids = [v.id for v in visible]
    assert version.id in visible_ids, f"descendants-shared version {version.id} should be visible from child project"


# ---------------------------------------------------------------------------
# Tests: Status categories and roadmap progress
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_roadmap_progress_counts_done_category(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    priority: IssuePriority,
    author: User,
) -> None:
    """Issues in 'done' and 'closed' categories count toward progress.

    4 issues: backlog, active, done, closed -> 2 of 4 = 50%.
    """
    statuses = {}
    for name, cat, pos in [
        ("Backlog", "backlog", 1),
        ("Active", "active", 2),
        ("Done", "done", 3),
        ("Terminal", "closed", 4),
    ]:
        s = StatusFactory.build(name=name, position=pos, category=cat)
        db_session.add(s)
        await db_session.flush()
        statuses[cat] = s

    await db_session.commit()
    version = await _make_version(db_session, project, name="cat-test")

    seq = 500
    for cat in ("backlog", "active", "done", "closed"):
        await _seed_issue(db_session, project, version, statuses[cat], tracker, priority, author, seq)
        seq += 1

    resp = await client.get(
        f"/api/v1/projects/{project.key}/roadmap/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    entries = resp.json()
    entry = next(e for e in entries if e["version"]["name"] == "cat-test")
    assert entry["closed_count"] == 2  # done + closed
    assert entry["open_count"] == 2  # backlog + active
    assert entry["progress_percent"] == 50


@pytest.mark.asyncio
async def test_roadmap_progress_resolved_counts_as_done(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    priority: IssuePriority,
    author: User,
) -> None:
    """Resolved (done category) issues count toward roadmap progress."""
    s_new = StatusFactory.build(name="NewR", position=1, category="backlog")
    s_resolved = StatusFactory.build(name="ResolvedR", position=3, category="done")
    db_session.add_all([s_new, s_resolved])
    await db_session.commit()

    version = await _make_version(db_session, project, name="resolved-test")

    # 1 new, 1 resolved -> 50%
    await _seed_issue(db_session, project, version, s_new, tracker, priority, author, 600)
    await _seed_issue(db_session, project, version, s_resolved, tracker, priority, author, 601)

    resp = await client.get(
        f"/api/v1/projects/{project.key}/roadmap/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    entries = resp.json()
    entry = next(e for e in entries if e["version"]["name"] == "resolved-test")
    assert entry["closed_count"] == 1
    assert entry["progress_percent"] == 50


@pytest.mark.asyncio
async def test_status_is_closed_property() -> None:
    """The is_closed property returns True only for category='closed'."""
    for cat, expected in [
        ("backlog", False),
        ("active", False),
        ("done", False),
        ("closed", True),
    ]:
        s = StatusFactory.build(category=cat)
        assert s.is_closed is expected, f"category={cat} -> is_closed should be {expected}"


@pytest.mark.asyncio
async def test_status_is_done_property() -> None:
    """The is_done property returns True for 'done' and 'closed' categories."""
    for cat, expected in [
        ("backlog", False),
        ("active", False),
        ("done", True),
        ("closed", True),
    ]:
        s = StatusFactory.build(category=cat)
        assert s.is_done is expected, f"category={cat} -> is_done should be {expected}"


# ---------------------------------------------------------------------------
# Tests: Version search endpoint (autocomplete)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_versions_empty_query_returns_recent_10(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
) -> None:
    """With no query, search returns up to 10 most recent versions, newest first."""
    for i in range(12):
        await _make_version(
            db_session,
            project,
            name=f"v{i:02d}",
            effective_date=datetime.date(2026, 1, 1) + datetime.timedelta(days=i),
        )

    resp = await client.get(
        f"/api/v1/projects/{project.key}/versions/search/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 10
    # Newest (highest effective_date) first
    assert data[0]["name"] == "v11"
    assert data[-1]["name"] == "v02"
    assert {"id", "name", "status", "effective_date"} <= set(data[0].keys())


@pytest.mark.asyncio
async def test_search_versions_query_substring_match(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
) -> None:
    """Non-empty query performs a case-insensitive substring match."""
    await _make_version(db_session, project, name="Release 1.0")
    await _make_version(db_session, project, name="Release 2.0")
    await _make_version(db_session, project, name="Hotfix 9.9")

    resp = await client.get(
        f"/api/v1/projects/{project.key}/versions/search/?q=release",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    names = {v["name"] for v in resp.json()}
    assert names == {"Release 1.0", "Release 2.0"}


@pytest.mark.asyncio
async def test_search_versions_returns_all_statuses(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
) -> None:
    """Search returns versions regardless of status (open/locked/closed)."""
    await _make_version(db_session, project, name="open-v", status="open")
    await _make_version(db_session, project, name="locked-v", status="locked")
    await _make_version(db_session, project, name="closed-v", status="closed")

    resp = await client.get(
        f"/api/v1/projects/{project.key}/versions/search/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    statuses = {v["status"] for v in resp.json()}
    assert statuses == {"open", "locked", "closed"}


@pytest.mark.asyncio
async def test_search_versions_requires_auth(
    client: AsyncClient,
    project: Project,
) -> None:
    """Unauthenticated users cannot search versions."""
    resp = await client.get(f"/api/v1/projects/{project.key}/versions/search/")
    assert resp.status_code == 401
