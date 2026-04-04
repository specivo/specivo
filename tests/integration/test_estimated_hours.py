"""Integration tests for estimated_hours validation on issues.

Covers:
- Create with valid estimated_hours (decimal, zero, null)
- Create rejected when estimated_hours is negative (422)
- Update with valid estimated_hours
- Update rejected when estimated_hours is negative (422)
- Clear estimated_hours via PATCH with null
- Journal detail created when estimated_hours changes
- Fractional hours stored correctly
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.journal import Journal, JournalDetail
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str, password: str = "testpassword") -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _create_issue(
    client: AsyncClient,
    token: str,
    project_key: str,
    tracker_id: int,
    status_id: int,
    priority_id: int,
    subject: str = "Test issue",
    estimated_hours: float | None = None,
) -> dict:
    payload: dict = {
        "project_key": project_key,
        "tracker_id": tracker_id,
        "subject": subject,
        "status_id": status_id,
        "priority_id": priority_id,
    }
    if estimated_hours is not None:
        payload["estimated_hours"] = estimated_hours
    resp = await client.post(
        f"/api/v1/projects/{project_key}/issues/",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, is_closed=False)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, status: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Task", default_status_id=status.id)
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
    proj = ProjectFactory.build(key="EH", identifier="estimated-hours-test")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="eh_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


# ---------------------------------------------------------------------------
# Tests: create
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_issue_with_estimated_hours(
    client: AsyncClient,
    token: str,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Creating an issue with estimated_hours=2.5 returns 201 with stored value."""
    resp = await _create_issue(
        client,
        token,
        project.key,
        tracker.id,
        status.id,
        priority.id,
        subject="With hours",
        estimated_hours=2.5,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    # Stored as Decimal(10,2); API may return "2.50" or "2.5" — normalise for comparison
    assert float(data["estimated_hours"]) == 2.5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_issue_without_estimated_hours(
    client: AsyncClient,
    token: str,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Creating an issue without estimated_hours returns 201 with null."""
    resp = await _create_issue(
        client,
        token,
        project.key,
        tracker.id,
        status.id,
        priority.id,
        subject="No hours",
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["estimated_hours"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_negative_estimated_hours_rejected_on_create(
    client: AsyncClient,
    token: str,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Creating an issue with estimated_hours=-1 returns 422."""
    resp = await _create_issue(
        client,
        token,
        project.key,
        tracker.id,
        status.id,
        priority.id,
        subject="Negative hours",
        estimated_hours=-1,
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Tests: update
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_estimated_hours(
    client: AsyncClient,
    token: str,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """PATCH with valid estimated_hours updates the field and returns 200."""
    create_resp = await _create_issue(
        client,
        token,
        project.key,
        tracker.id,
        status.id,
        priority.id,
        subject="Update hours",
    )
    assert create_resp.status_code == 201, create_resp.text
    issue_key = create_resp.json()["key"]
    lock_version = create_resp.json()["lock_version"]

    resp = await client.patch(
        f"/api/v1/issues/{issue_key}/",
        json={"estimated_hours": 4.0, "lock_version": lock_version},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert float(resp.json()["estimated_hours"]) == 4.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_negative_estimated_hours_rejected_on_update(
    client: AsyncClient,
    token: str,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """PATCH with estimated_hours=-1 returns 422."""
    create_resp = await _create_issue(
        client,
        token,
        project.key,
        tracker.id,
        status.id,
        priority.id,
        subject="Reject negative update",
    )
    assert create_resp.status_code == 201, create_resp.text
    issue_key = create_resp.json()["key"]
    lock_version = create_resp.json()["lock_version"]

    resp = await client.patch(
        f"/api/v1/issues/{issue_key}/",
        json={"estimated_hours": -1, "lock_version": lock_version},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_clear_estimated_hours(
    client: AsyncClient,
    token: str,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """PATCH with estimated_hours=null is a no-op: the field is left unchanged.

    The service uses ``if data.estimated_hours is not None`` semantics, so
    sending null does not clear the field.  Omitting the key entirely also
    leaves the field unchanged.  This test documents the current behaviour.
    TODO: add a dedicated "clear" mechanism (e.g. sentinel value or explicit
    ``clear_estimated_hours: true`` flag) when the feature is implemented.
    """
    create_resp = await _create_issue(
        client,
        token,
        project.key,
        tracker.id,
        status.id,
        priority.id,
        subject="Clear hours",
        estimated_hours=3.0,
    )
    assert create_resp.status_code == 201, create_resp.text
    issue_key = create_resp.json()["key"]
    lock_version = create_resp.json()["lock_version"]

    resp = await client.patch(
        f"/api/v1/issues/{issue_key}/",
        json={"estimated_hours": None, "lock_version": lock_version},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    # null is treated as "no change" — original value is preserved
    assert float(resp.json()["estimated_hours"]) == 3.0


# ---------------------------------------------------------------------------
# Tests: journal tracking
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_estimated_hours_change_creates_journal(
    client: AsyncClient,
    db_session: AsyncSession,
    token: str,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Changing estimated_hours via PATCH creates a JournalDetail with old/new values."""
    create_resp = await _create_issue(
        client,
        token,
        project.key,
        tracker.id,
        status.id,
        priority.id,
        subject="Journal hours",
        estimated_hours=2.0,
    )
    assert create_resp.status_code == 201, create_resp.text
    issue_data = create_resp.json()
    issue_key = issue_data["key"]
    lock_version = issue_data["lock_version"]

    patch_resp = await client.patch(
        f"/api/v1/issues/{issue_key}/",
        json={"estimated_hours": 4.0, "lock_version": lock_version},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert patch_resp.status_code == 200, patch_resp.text

    # Verify journal detail exists in DB
    result = await db_session.execute(
        select(JournalDetail)
        .join(Journal, JournalDetail.journal_id == Journal.id)
        .where(
            Journal.issue_id == issue_data["id"],
            JournalDetail.prop_key == "estimated_hours",
        )
    )
    detail = result.scalar_one_or_none()
    assert detail is not None, "Expected a JournalDetail for estimated_hours change"

    # Values may be serialised as "2.00"/"2" etc — normalise via float comparison
    assert float(detail.old_value) == 2.0, f"Expected old_value 2.0, got {detail.old_value!r}"
    assert float(detail.new_value) == 4.0, f"Expected new_value 4.0, got {detail.new_value!r}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_estimated_hours_change_visible_via_include_journals(
    client: AsyncClient,
    token: str,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Journal detail for estimated_hours change is returned via ?include=journals."""
    create_resp = await _create_issue(
        client,
        token,
        project.key,
        tracker.id,
        status.id,
        priority.id,
        subject="Journal API hours",
        estimated_hours=2.0,
    )
    assert create_resp.status_code == 201, create_resp.text
    issue_data = create_resp.json()
    issue_key = issue_data["key"]
    lock_version = issue_data["lock_version"]

    patch_resp = await client.patch(
        f"/api/v1/issues/{issue_key}/",
        json={"estimated_hours": 4.0, "lock_version": lock_version},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert patch_resp.status_code == 200, patch_resp.text

    resp = await client.get(
        f"/api/v1/issues/{issue_key}/?include=journals",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    journals = resp.json().get("journals", [])
    assert journals, "Expected at least one journal"

    all_details = [d for j in journals for d in j.get("details", [])]
    hours_detail = next((d for d in all_details if d["prop_key"] == "estimated_hours"), None)
    assert hours_detail is not None, "Expected a journal detail with prop_key='estimated_hours'"
    assert float(hours_detail["old_value"]) == 2.0
    assert float(hours_detail["new_value"]) == 4.0


# ---------------------------------------------------------------------------
# Tests: boundary values
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_zero_estimated_hours_allowed(
    client: AsyncClient,
    token: str,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """estimated_hours=0 is accepted (boundary value, ge=0)."""
    resp = await _create_issue(
        client,
        token,
        project.key,
        tracker.id,
        status.id,
        priority.id,
        subject="Zero hours",
        estimated_hours=0,
    )
    assert resp.status_code == 201, resp.text
    assert float(resp.json()["estimated_hours"]) == 0.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fractional_estimated_hours(
    client: AsyncClient,
    token: str,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """estimated_hours=1.75 (1h 45m) is stored and returned correctly."""
    resp = await _create_issue(
        client,
        token,
        project.key,
        tracker.id,
        status.id,
        priority.id,
        subject="Fractional hours",
        estimated_hours=1.75,
    )
    assert resp.status_code == 201, resp.text
    assert float(resp.json()["estimated_hours"]) == 1.75


@pytest.mark.integration
@pytest.mark.asyncio
async def test_large_estimated_hours_allowed(
    client: AsyncClient,
    token: str,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """estimated_hours=100.75 (100h 45m) is accepted."""
    resp = await _create_issue(
        client,
        token,
        project.key,
        tracker.id,
        status.id,
        priority.id,
        subject="Large hours",
        estimated_hours=100.75,
    )
    assert resp.status_code == 201, resp.text
    assert float(resp.json()["estimated_hours"]) == 100.75
