"""Integration tests for Time Tracking API.

Covers:
- POST   /api/v1/projects/{key}/time-entries  (create)
- GET    /api/v1/projects/{key}/time-entries  (list for project)
- GET    /api/v1/time-entries/{id}            (get single)
- PATCH  /api/v1/time-entries/{id}            (update)
- DELETE /api/v1/time-entries/{id}            (delete)
- GET    /api/v1/time-entries/activities       (list activities)
- POST   /api/v1/timer/start                  (start timer)
- POST   /api/v1/timer/stop                   (stop timer)
- GET    /api/v1/timer                        (get current timer)
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.time_entry import TimeEntryActivity
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.time_entry import TimeEntryActivityFactory, TimeEntryFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str, password: str = TEST_PASSWORD) -> str:
    resp = await client.post("/api/v1/auth/login", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def open_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, is_closed=False)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, open_status: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Bug", default_status_id=open_status.id)
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
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="ACME", identifier="acme-app")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def activity(db_session: AsyncSession) -> TimeEntryActivity:
    a = TimeEntryActivityFactory.build(name="Development", position=1, is_default=True)
    db_session.add(a)
    await db_session.commit()
    await db_session.refresh(a)
    return a


@pytest_asyncio.fixture
async def activity2(db_session: AsyncSession) -> TimeEntryActivity:
    a = TimeEntryActivityFactory.build(name="Testing", position=2, is_default=False)
    db_session.add(a)
    await db_session.commit()
    await db_session.refresh(a)
    return a


@pytest_asyncio.fixture
async def issue(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
    admin_user: User,
) -> dict:
    """Create an issue via direct DB insert and return its data as dict."""
    from tests.factories.issue import IssueFactory

    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=open_status.id,
        priority_id=priority.id,
        author_id=admin_user.id,
        subject="Test issue for time tracking",
    )
    db_session.add(issue)
    await db_session.commit()
    await db_session.refresh(issue)
    return {"id": issue.id, "key": issue.display_key}


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="time_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def regular_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="time_regular", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


@pytest_asyncio.fixture
async def regular_token(regular_user: User, client: AsyncClient) -> str:
    return await _login(client, regular_user.login)


# ---------------------------------------------------------------------------
# Tests: CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_time_entry(
    client: AsyncClient,
    admin_token: str,
    project: Project,
    activity: TimeEntryActivity,
    issue: dict,
) -> None:
    """POST with hours, activity, issue -> 201."""
    resp = await client.post(
        f"/api/v1/projects/{project.key}/time-entries",
        json={
            "issue_id": issue["id"],
            "activity_id": activity.id,
            "hours": "2.50",
            "comments": "Implemented feature X",
            "spent_on": "2026-03-22",
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["hours"] == "2.50"
    assert data["issue_id"] == issue["id"]
    assert data["project_id"] == project.id
    assert data["comments"] == "Implemented feature X"
    assert data["is_billable"] is False


@pytest.mark.asyncio
async def test_create_time_entry_project_level(
    client: AsyncClient,
    admin_token: str,
    project: Project,
    activity: TimeEntryActivity,
) -> None:
    """POST without issue_id -> 201 (project-level time entry)."""
    resp = await client.post(
        f"/api/v1/projects/{project.key}/time-entries",
        json={
            "activity_id": activity.id,
            "hours": "1.00",
            "spent_on": "2026-03-22",
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["issue_id"] is None
    assert data["project_id"] == project.id


@pytest.mark.asyncio
async def test_list_time_entries_for_project(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    admin_user: User,
    project: Project,
    activity: TimeEntryActivity,
) -> None:
    """GET entries for project."""
    # Create entries directly in DB
    for i in range(3):
        entry = TimeEntryFactory.build(
            project_id=project.id,
            user_id=admin_user.id,
            activity_id=activity.id,
            hours=Decimal(f"{i + 1}.00"),
            spent_on=date(2026, 3, 22),
        )
        db_session.add(entry)
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{project.key}/time-entries",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] == 3
    assert len(data["items"]) == 3


@pytest.mark.asyncio
async def test_list_time_entries_filter_by_user(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    admin_user: User,
    regular_user: User,
    project: Project,
    activity: TimeEntryActivity,
) -> None:
    """Filter by user_id."""
    # Create entries for both users
    for uid in [admin_user.id, regular_user.id]:
        entry = TimeEntryFactory.build(
            project_id=project.id,
            user_id=uid,
            activity_id=activity.id,
            spent_on=date(2026, 3, 22),
        )
        db_session.add(entry)
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{project.key}/time-entries?user_id={admin_user.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] == 1
    assert data["items"][0]["user"]["id"] == admin_user.id


@pytest.mark.asyncio
async def test_list_time_entries_filter_by_date(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    admin_user: User,
    project: Project,
    activity: TimeEntryActivity,
) -> None:
    """Filter by spent_on range."""
    for d in [date(2026, 3, 20), date(2026, 3, 22), date(2026, 3, 25)]:
        entry = TimeEntryFactory.build(
            project_id=project.id,
            user_id=admin_user.id,
            activity_id=activity.id,
            spent_on=d,
        )
        db_session.add(entry)
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{project.key}/time-entries?from_date=2026-03-21&to_date=2026-03-22",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] == 1
    assert data["items"][0]["spent_on"] == "2026-03-22"


@pytest.mark.asyncio
async def test_get_time_entry(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    admin_user: User,
    project: Project,
    activity: TimeEntryActivity,
) -> None:
    """GET single entry -> 200."""
    entry = TimeEntryFactory.build(
        project_id=project.id,
        user_id=admin_user.id,
        activity_id=activity.id,
        spent_on=date(2026, 3, 22),
        comments="Test entry",
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    resp = await client.get(
        f"/api/v1/time-entries/{entry.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == entry.id
    assert data["comments"] == "Test entry"


@pytest.mark.asyncio
async def test_update_time_entry(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    admin_user: User,
    project: Project,
    activity: TimeEntryActivity,
) -> None:
    """PATCH hours/comment -> updated."""
    entry = TimeEntryFactory.build(
        project_id=project.id,
        user_id=admin_user.id,
        activity_id=activity.id,
        spent_on=date(2026, 3, 22),
        hours=Decimal("1.00"),
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    resp = await client.patch(
        f"/api/v1/time-entries/{entry.id}",
        json={"hours": "3.50", "comments": "Updated time"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["hours"] == "3.50"
    assert data["comments"] == "Updated time"


@pytest.mark.asyncio
async def test_delete_own_time_entry(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    admin_user: User,
    project: Project,
    activity: TimeEntryActivity,
) -> None:
    """DELETE own entry -> 204."""
    entry = TimeEntryFactory.build(
        project_id=project.id,
        user_id=admin_user.id,
        activity_id=activity.id,
        spent_on=date(2026, 3, 22),
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    resp = await client.delete(
        f"/api/v1/time-entries/{entry.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_others_entry_as_admin(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    regular_user: User,
    project: Project,
    activity: TimeEntryActivity,
) -> None:
    """Admin can delete others' entries -> 204."""
    entry = TimeEntryFactory.build(
        project_id=project.id,
        user_id=regular_user.id,
        activity_id=activity.id,
        spent_on=date(2026, 3, 22),
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    resp = await client.delete(
        f"/api/v1/time-entries/{entry.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_others_entry_as_regular_user(
    client: AsyncClient,
    db_session: AsyncSession,
    regular_token: str,
    admin_user: User,
    project: Project,
    activity: TimeEntryActivity,
) -> None:
    """Regular user cannot delete others' entries -> 403."""
    entry = TimeEntryFactory.build(
        project_id=project.id,
        user_id=admin_user.id,
        activity_id=activity.id,
        spent_on=date(2026, 3, 22),
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    resp = await client.delete(
        f"/api/v1/time-entries/{entry.id}",
        headers={"Authorization": f"Bearer {regular_token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_activities(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    activity: TimeEntryActivity,
    activity2: TimeEntryActivity,
) -> None:
    """GET activities -> list with seed data."""
    resp = await client.get(
        "/api/v1/time-entries/activities",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) >= 2
    names = [a["name"] for a in data]
    assert "Development" in names
    assert "Testing" in names


@pytest.mark.asyncio
async def test_create_time_entry_invalid_activity(
    client: AsyncClient,
    admin_token: str,
    project: Project,
) -> None:
    """Nonexistent activity_id -> 422."""
    resp = await client.post(
        f"/api/v1/projects/{project.key}/time-entries",
        json={
            "activity_id": 99999,
            "hours": "1.00",
            "spent_on": "2026-03-22",
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_time_entry_billable_flag(
    client: AsyncClient,
    admin_token: str,
    project: Project,
    activity: TimeEntryActivity,
) -> None:
    """Set is_billable=true, verify in response."""
    resp = await client.post(
        f"/api/v1/projects/{project.key}/time-entries",
        json={
            "activity_id": activity.id,
            "hours": "1.00",
            "spent_on": "2026-03-22",
            "is_billable": True,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_billable"] is True


# ---------------------------------------------------------------------------
# Tests: Timer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_timer(
    client: AsyncClient,
    admin_token: str,
    project: Project,
) -> None:
    """POST /timer/start -> timer created."""
    resp = await client.post(
        "/api/v1/timer/start",
        json={"project_id": project.id},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["project_id"] == project.id
    assert data["issue_id"] is None
    assert "elapsed_seconds" in data
    assert data["elapsed_seconds"] >= 0


@pytest.mark.asyncio
async def test_get_current_timer(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    admin_user: User,
    project: Project,
) -> None:
    """GET /timer -> current timer with elapsed_seconds."""
    from tests.factories.time_entry import ActiveTimerFactory

    timer = ActiveTimerFactory.build(
        user_id=admin_user.id,
        project_id=project.id,
        started_at=datetime.now(UTC) - timedelta(minutes=30),
    )
    db_session.add(timer)
    await db_session.commit()

    resp = await client.get(
        "/api/v1/timer",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["project_id"] == project.id
    # Should be roughly 30 minutes = 1800 seconds (allow some slack)
    assert data["elapsed_seconds"] >= 1700


@pytest.mark.asyncio
async def test_stop_timer(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    admin_user: User,
    project: Project,
    activity: TimeEntryActivity,
) -> None:
    """POST /timer/stop -> time entry created, timer deleted."""
    from tests.factories.time_entry import ActiveTimerFactory

    timer = ActiveTimerFactory.build(
        user_id=admin_user.id,
        project_id=project.id,
        started_at=datetime.now(UTC) - timedelta(hours=2),
    )
    db_session.add(timer)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/timer/stop",
        json={"activity_id": activity.id},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "hours" in data
    # Should be roughly 2.0 hours
    hours = float(data["hours"])
    assert 1.9 <= hours <= 2.1

    # Timer should be gone
    resp2 = await client.get(
        "/api/v1/timer",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp2.status_code == 200
    assert resp2.json() is None


@pytest.mark.asyncio
async def test_start_timer_stops_previous(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    admin_user: User,
    project: Project,
    activity: TimeEntryActivity,
) -> None:
    """Start new timer -> old one auto-stopped + logged."""
    from tests.factories.time_entry import ActiveTimerFactory

    timer = ActiveTimerFactory.build(
        user_id=admin_user.id,
        project_id=project.id,
        started_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add(timer)
    await db_session.commit()

    # Start a new timer — old one should be auto-stopped
    resp = await client.post(
        "/api/v1/timer/start",
        json={"project_id": project.id, "comments": "New task"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text

    # The old timer should have created a time entry
    resp2 = await client.get(
        f"/api/v1/projects/{project.key}/time-entries",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp2.status_code == 200
    entries = resp2.json()["items"]
    assert len(entries) >= 1


@pytest.mark.asyncio
async def test_stop_timer_no_active(
    client: AsyncClient,
    admin_token: str,
) -> None:
    """Stop when no timer -> 404."""
    resp = await client.post(
        "/api/v1/timer/stop",
        json={"activity_id": 1},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_timer_requires_auth(
    client: AsyncClient,
) -> None:
    """No auth -> 401."""
    resp = await client.get("/api/v1/timer")
    assert resp.status_code == 401

    resp2 = await client.post("/api/v1/timer/start", json={"project_id": 1})
    assert resp2.status_code == 401

    resp3 = await client.post("/api/v1/timer/stop", json={"activity_id": 1})
    assert resp3.status_code == 401
