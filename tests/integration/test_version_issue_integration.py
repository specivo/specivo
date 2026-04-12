"""Integration tests for issue ↔ version integration (Phase 3: fixed_version_id).

Tests cover:
- IssueCreate with fixed_version_id (schema not yet exposed — TDD red phase)
- IssueUpdate to assign / clear fixed_version_id (schema not yet exposed)
- IssueOut including version info (not yet in response)
- Cross-project version rejection
- Invalid version_id rejection
- Roadmap count consistency when issues are assigned via API
- Filter issues by version_id (not yet in list endpoint)
- Cascade SET NULL when version is deleted
- Locked version rejects new issue assignment

All tests in this file are expected to FAIL until the following work is done:
- Add ``fixed_version_id`` to IssueCreate schema
- Add ``fixed_version_id`` to IssueUpdate schema
- Add ``fixed_version`` to IssueOut response
- Add ``?version_id=N`` filter support to the issues list endpoint
- Add locked-version guard in IssueService.create / update
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from specivo.models.version import Version
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory
from tests.factories.version import VersionFactory

# ---------------------------------------------------------------------------
# Helpers shared across this module
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str, password: str = "testpassword") -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _create_issue_via_api(
    client: AsyncClient,
    token: str,
    project_key: str,
    tracker_id: int,
    status_id: int,
    priority_id: int,
    subject: str,
    *,
    fixed_version_id: int | None = None,
) -> dict:
    """POST to create an issue; optionally include fixed_version_id."""
    payload: dict = {
        "project_key": project_key,
        "tracker_id": tracker_id,
        "subject": subject,
        "status_id": status_id,
        "priority_id": priority_id,
    }
    if fixed_version_id is not None:
        payload["fixed_version_id"] = fixed_version_id

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
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="vi_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


@pytest_asyncio.fixture
async def open_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def closed_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="Closed", position=5, category="closed")
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
async def project_a(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="VIA", identifier="vi-project-a")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def project_b(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="VIB", identifier="vi-project-b")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def open_version(db_session: AsyncSession, project_a: Project) -> Version:
    v = VersionFactory.build(project_id=project_a.id, name="v1.0", status="open")
    db_session.add(v)
    await db_session.commit()
    await db_session.refresh(v)
    return v


@pytest_asyncio.fixture
async def locked_version(db_session: AsyncSession, project_a: Project) -> Version:
    v = VersionFactory.build(project_id=project_a.id, name="v0.9-locked", status="locked")
    db_session.add(v)
    await db_session.commit()
    await db_session.refresh(v)
    return v


@pytest_asyncio.fixture
async def version_in_b(db_session: AsyncSession, project_b: Project) -> Version:
    v = VersionFactory.build(project_id=project_b.id, name="v2.0-b", status="open")
    db_session.add(v)
    await db_session.commit()
    await db_session.refresh(v)
    return v


# ---------------------------------------------------------------------------
# Tests: IssueCreate with fixed_version_id (FAILS until schema updated)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_issue_with_fixed_version(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project_a: Project,
    open_version: Version,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """POST issue with fixed_version_id — field must be persisted and returned.

    RED: IssueCreate schema does not yet have fixed_version_id.
    The created issue must:
    - return 201
    - include fixed_version_id (or version info) in the response
    - have fixed_version_id stored in the DB
    """
    resp = await _create_issue_via_api(
        client,
        admin_token,
        project_a.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Issue with fixed version",
        fixed_version_id=open_version.id,
    )
    assert resp.status_code == 201, resp.text

    data = resp.json()
    # Response must expose the version assignment — either as fixed_version_id
    # or as a nested fixed_version object.
    assert data.get("fixed_version_id") == open_version.id or (
        data.get("fixed_version") is not None and data["fixed_version"]["id"] == open_version.id
    ), f"fixed_version_id not found in response: {data}"

    # Verify DB persistence
    result = await db_session.execute(select(Issue).where(Issue.id == data["id"]))
    issue = result.scalar_one()
    assert issue.fixed_version_id == open_version.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_update_issue_fixed_version(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project_a: Project,
    open_version: Version,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """PATCH issue to assign a version — fixed_version_id must be updated.

    RED: IssueUpdate schema does not yet have fixed_version_id.
    """
    # Create issue without a version
    create_resp = await _create_issue_via_api(
        client,
        admin_token,
        project_a.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Issue to assign version",
    )
    assert create_resp.status_code == 201, create_resp.text
    issue_data = create_resp.json()
    issue_key = issue_data["key"]
    lock_version = issue_data["lock_version"]

    # Assign the version via PATCH
    patch_resp = await client.patch(
        f"/api/v1/issues/{issue_key}/",
        json={"fixed_version_id": open_version.id, "lock_version": lock_version},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert patch_resp.status_code == 200, patch_resp.text

    data = patch_resp.json()
    assert data.get("fixed_version_id") == open_version.id or (
        data.get("fixed_version") is not None and data["fixed_version"]["id"] == open_version.id
    ), f"fixed_version_id not reflected in PATCH response: {data}"

    # Verify in DB
    await db_session.refresh(await db_session.get(Issue, issue_data["id"]))
    result = await db_session.execute(select(Issue).where(Issue.id == issue_data["id"]))
    issue = result.scalar_one()
    assert issue.fixed_version_id == open_version.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_update_issue_clear_fixed_version(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project_a: Project,
    open_version: Version,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """PATCH issue to remove version assignment (set to null).

    RED: IssueUpdate schema does not yet have fixed_version_id.
    Sending fixed_version_id=null must clear the field.
    """
    # Create with a version
    create_resp = await _create_issue_via_api(
        client,
        admin_token,
        project_a.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Issue to clear version",
        fixed_version_id=open_version.id,
    )
    assert create_resp.status_code == 201, create_resp.text
    issue_data = create_resp.json()
    issue_key = issue_data["key"]
    lock_version = issue_data["lock_version"]

    # Clear the version
    patch_resp = await client.patch(
        f"/api/v1/issues/{issue_key}/",
        json={"fixed_version_id": None, "lock_version": lock_version},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert patch_resp.status_code == 200, patch_resp.text

    data = patch_resp.json()
    # fixed_version_id must be null / absent in the response
    assert data.get("fixed_version_id") is None, f"Expected fixed_version_id=null, got: {data}"
    if "fixed_version" in data:
        assert data["fixed_version"] is None, f"Expected fixed_version=null, got: {data}"

    # Verify in DB
    result = await db_session.execute(select(Issue).where(Issue.id == issue_data["id"]))
    issue = result.scalar_one()
    assert issue.fixed_version_id is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_issue_response_includes_version_info(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project_a: Project,
    open_version: Version,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """GET issue must return version name and status in the response.

    RED: IssueOut does not yet include version info.
    Expected shape (one of):
      - ``fixed_version_id: int`` (minimal)
      - ``fixed_version: {id, name, status}`` (richer)
    """
    create_resp = await _create_issue_via_api(
        client,
        admin_token,
        project_a.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Issue for version info test",
        fixed_version_id=open_version.id,
    )
    assert create_resp.status_code == 201, create_resp.text
    issue_key = create_resp.json()["key"]

    get_resp = await client.get(
        f"/api/v1/issues/{issue_key}/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert get_resp.status_code == 200, get_resp.text
    data = get_resp.json()

    # Must expose the version — either minimal id or rich object
    has_version_id = data.get("fixed_version_id") == open_version.id
    has_version_obj = (
        isinstance(data.get("fixed_version"), dict)
        and data["fixed_version"].get("id") == open_version.id
        and data["fixed_version"].get("name") == open_version.name
    )
    assert has_version_id or has_version_obj, f"Issue GET response does not include version info. Response: {data}"


# ---------------------------------------------------------------------------
# Tests: validation / rejection (FAILS until service-layer guards added)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_issue_with_invalid_version_rejects(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project_a: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """POST with non-existent fixed_version_id must return 400 or 422.

    RED: No validation for fixed_version_id in IssueService.create.
    """
    resp = await _create_issue_via_api(
        client,
        admin_token,
        project_a.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Issue with bad version",
        fixed_version_id=999999,  # Does not exist
    )
    assert resp.status_code in (400, 422), (
        f"Expected 400/422 for invalid version_id, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_issue_with_version_from_other_project_rejects(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project_a: Project,
    version_in_b: Version,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """POST issue in project A with a version belonging to project B must be rejected.

    RED: No cross-project version ownership check in IssueService.create.
    version_in_b has sharing='none', so it must not be assignable to project A.
    """
    resp = await _create_issue_via_api(
        client,
        admin_token,
        project_a.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Issue with cross-project version",
        fixed_version_id=version_in_b.id,
    )
    assert resp.status_code in (400, 422), (
        f"Expected 400/422 for cross-project version, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_locked_version_rejects_new_issues(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project_a: Project,
    locked_version: Version,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Assigning a new issue to a locked version must return 400/422.

    RED: No locked-version guard in IssueService.create.
    Locked versions should not accept new issue assignments.
    """
    resp = await _create_issue_via_api(
        client,
        admin_token,
        project_a.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Issue assigned to locked version",
        fixed_version_id=locked_version.id,
    )
    assert resp.status_code in (400, 422), (
        f"Expected 400/422 when assigning to locked version, got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# Tests: roadmap consistency (depends on fixed_version_id round-trip working)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_roadmap_counts_reflect_issue_assignments(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project_a: Project,
    open_version: Version,
    tracker: Tracker,
    open_status: IssueStatus,
    closed_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Assign issues via the API, then verify roadmap open/closed counts.

    RED: Requires fixed_version_id in IssueCreate to work.
    Creates 2 open issues and 1 closed issue via the API, then checks roadmap.
    """
    # Create 2 open issues through the API with fixed_version_id
    for i in range(2):
        resp = await _create_issue_via_api(
            client,
            admin_token,
            project_a.key,
            tracker.id,
            open_status.id,
            priority.id,
            f"Open issue {i}",
            fixed_version_id=open_version.id,
        )
        assert resp.status_code == 201, f"Failed to create open issue {i}: {resp.text}"

    # Create 1 closed issue
    closed_resp = await _create_issue_via_api(
        client,
        admin_token,
        project_a.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Closed issue",
        fixed_version_id=open_version.id,
    )
    assert closed_resp.status_code == 201, closed_resp.text
    closed_issue_key = closed_resp.json()["key"]
    closed_lv = closed_resp.json()["lock_version"]

    # Close it via PATCH
    patch_resp = await client.patch(
        f"/api/v1/issues/{closed_issue_key}/",
        json={"status_id": closed_status.id, "lock_version": closed_lv},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert patch_resp.status_code == 200, patch_resp.text

    # Verify roadmap reflects the counts
    roadmap_resp = await client.get(
        f"/api/v1/projects/{project_a.key}/roadmap/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert roadmap_resp.status_code == 200, roadmap_resp.text
    entries = roadmap_resp.json()
    assert len(entries) == 1

    entry = entries[0]
    assert entry["version"]["id"] == open_version.id
    assert entry["open_count"] == 2
    assert entry["closed_count"] == 1
    assert entry["total"] == 3


# ---------------------------------------------------------------------------
# Tests: filter issues by version_id (FAILS until filter added)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_filter_issues_by_version(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project_a: Project,
    open_version: Version,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """GET /projects/{key}/issues/?version_id=N returns only issues assigned to that version.

    RED: The list endpoint does not yet support ?version_id filter.
    Two issues are created: one assigned to open_version, one unassigned.
    The filter must return exactly one result.
    """
    # Create issue assigned to open_version
    resp_with = await _create_issue_via_api(
        client,
        admin_token,
        project_a.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Versioned issue",
        fixed_version_id=open_version.id,
    )
    assert resp_with.status_code == 201, resp_with.text

    # Create issue without a version
    resp_without = await _create_issue_via_api(
        client,
        admin_token,
        project_a.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Unversioned issue",
    )
    assert resp_without.status_code == 201, resp_without.text

    # Filter by version
    list_resp = await client.get(
        f"/api/v1/projects/{project_a.key}/issues/",
        params={"version_id": open_version.id, "status": "all"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert list_resp.status_code == 200, list_resp.text
    body = list_resp.json()

    items = body["items"]
    assert len(items) == 1, f"Expected 1 issue filtered by version, got {len(items)}: {items}"
    assert items[0]["subject"] == "Versioned issue"


# ---------------------------------------------------------------------------
# Tests: cascade SET NULL on version delete (partially implemented via FK)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_delete_version_nullifies_issue_fixed_version(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project_a: Project,
    open_version: Version,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
    admin_user: User,
) -> None:
    """Deleting a version sets fixed_version_id to NULL on all assigned issues.

    The FK is defined with ondelete='SET NULL' so this should work at the DB
    level. This test verifies the behaviour end-to-end:
    1. Create issue with fixed_version_id set (via direct DB insertion to
       avoid dependency on the schema fix — we seed the Issue directly)
    2. Delete the version via the API
    3. Verify the issue's fixed_version_id is NULL
    """
    # Seed the issue directly so this test does not depend on the IssueCreate
    # schema change (which is tested separately above).
    from tests.factories.issue import IssueFactory

    issue = IssueFactory.build(
        project_id=project_a.id,
        project_key=project_a.key,
        sequence_number=900,
        tracker_id=tracker.id,
        status_id=open_status.id,
        priority_id=priority.id,
        author_id=admin_user.id,
        subject="Issue linked to version",
        fixed_version_id=open_version.id,
    )
    db_session.add(issue)
    await db_session.commit()
    await db_session.refresh(issue)
    assert issue.fixed_version_id == open_version.id

    # Delete the version via the API
    del_resp = await client.delete(
        f"/api/v1/projects/{project_a.key}/versions/{open_version.id}/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert del_resp.status_code == 204, del_resp.text

    # Reload the issue and verify SET NULL happened
    await db_session.refresh(issue)
    assert issue.fixed_version_id is None, (
        f"Expected fixed_version_id=NULL after version deletion, got {issue.fixed_version_id}"
    )
