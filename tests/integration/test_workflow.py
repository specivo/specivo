"""Integration tests for workflow engine — transition validation on issue updates.

Covers:
- Valid transitions succeed
- Invalid transitions return 422 with workflow_transition_denied
- Admin bypasses workflow validation
- No rules = allow any transition (backward compat)
- GET /issues/{ref}/allowed-statuses endpoint
- Required field validation on transition
- Readonly field validation on transition
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.workflow import WorkflowFieldRule, WorkflowTransition
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

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
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project_key}/issues/",
        json={
            "project_key": project_key,
            "tracker_id": tracker_id,
            "subject": subject,
            "status_id": status_id,
            "priority_id": priority_id,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def new_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, is_closed=False)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def in_progress_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="In Progress", position=2, is_closed=False)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def resolved_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="Resolved", position=3, is_closed=False)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def closed_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="Closed", position=5, is_closed=True)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def rejected_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="Rejected", position=6, is_closed=True)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, new_status: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Bug", default_status_id=new_status.id)
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
async def developer_role(db_session: AsyncSession) -> Role:
    result = await db_session.execute(select(Role).where(Role.name == "Developer"))
    existing = result.scalar_one_or_none()
    if existing:
        return existing
    role = Role(
        name="Developer",
        position=2,
        assignable=True,
        builtin=0,
        permissions=["add_issues", "edit_issues", "add_issue_notes", "view_issues"],
        issues_visibility="default",
        settings={},
    )
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="WF", identifier="wf-test")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def dev_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="wf_dev", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def dev_member(
    db_session: AsyncSession,
    project: Project,
    dev_user: User,
    developer_role: Role,
) -> Member:
    """Create project membership with Developer role."""
    member = Member(user_id=dev_user.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    mr = MemberRole(member_id=member.id, role_id=developer_role.id)
    db_session.add(mr)
    await db_session.commit()
    await db_session.refresh(member)
    return member


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="wf_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def workflow_rules(
    db_session: AsyncSession,
    tracker: Tracker,
    developer_role: Role,
    new_status: IssueStatus,
    in_progress_status: IssueStatus,
    resolved_status: IssueStatus,
    closed_status: IssueStatus,
    rejected_status: IssueStatus,
) -> list[WorkflowTransition]:
    """Set up workflow transitions: New->InProgress, New->Rejected,
    InProgress->Resolved, Resolved->Closed."""
    transitions_data = [
        (new_status.id, in_progress_status.id),
        (new_status.id, rejected_status.id),
        (in_progress_status.id, resolved_status.id),
        (resolved_status.id, closed_status.id),
    ]
    transitions = []
    for old_id, new_id in transitions_data:
        t = WorkflowTransition(
            tracker_id=tracker.id,
            role_id=developer_role.id,
            old_status_id=old_id,
            new_status_id=new_id,
        )
        db_session.add(t)
        transitions.append(t)
    await db_session.commit()
    for t in transitions:
        await db_session.refresh(t)
    return transitions


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_transition_succeeds(
    client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    new_status: IssueStatus,
    in_progress_status: IssueStatus,
    priority: IssuePriority,
    dev_user: User,
    dev_member: Member,
    workflow_rules: list[WorkflowTransition],
) -> None:
    """Update issue status along allowed path (New -> In Progress) succeeds."""
    token = await _login(client, dev_user.login)
    issue = await _create_issue(client, token, project.key, tracker.id, new_status.id, priority.id)

    resp = await client.patch(
        f"/api/v1/issues/{issue['key']}/",
        json={
            "status_id": in_progress_status.id,
            "lock_version": issue["lock_version"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"]["id"] == in_progress_status.id


@pytest.mark.asyncio
async def test_invalid_transition_returns_422(
    client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    new_status: IssueStatus,
    closed_status: IssueStatus,
    priority: IssuePriority,
    dev_user: User,
    dev_member: Member,
    workflow_rules: list[WorkflowTransition],
) -> None:
    """Attempt disallowed transition (New -> Closed) returns 422."""
    token = await _login(client, dev_user.login)
    issue = await _create_issue(client, token, project.key, tracker.id, new_status.id, priority.id)

    resp = await client.patch(
        f"/api/v1/issues/{issue['key']}/",
        json={
            "status_id": closed_status.id,
            "lock_version": issue["lock_version"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    error = body["errors"][0]
    assert error["code"] == "workflow_transition_denied"
    assert "allowed_status_ids" in error["details"]


@pytest.mark.asyncio
async def test_admin_bypasses_workflow(
    client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    new_status: IssueStatus,
    closed_status: IssueStatus,
    priority: IssuePriority,
    admin_user: User,
    workflow_rules: list[WorkflowTransition],
) -> None:
    """Admin can set any status regardless of workflow rules."""
    token = await _login(client, admin_user.login)
    issue = await _create_issue(client, token, project.key, tracker.id, new_status.id, priority.id)

    # New -> Closed is not in workflow rules, but admin bypasses
    resp = await client.patch(
        f"/api/v1/issues/{issue['key']}/",
        json={
            "status_id": closed_status.id,
            "lock_version": issue["lock_version"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"]["id"] == closed_status.id


@pytest.mark.asyncio
async def test_no_rules_allows_any_transition(
    client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    new_status: IssueStatus,
    closed_status: IssueStatus,
    priority: IssuePriority,
    dev_user: User,
    dev_member: Member,
    # Note: NO workflow_rules fixture — table is empty
) -> None:
    """When workflow_transitions is empty, any status change works (backward compat)."""
    token = await _login(client, dev_user.login)
    issue = await _create_issue(client, token, project.key, tracker.id, new_status.id, priority.id)

    resp = await client.patch(
        f"/api/v1/issues/{issue['key']}/",
        json={
            "status_id": closed_status.id,
            "lock_version": issue["lock_version"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"]["id"] == closed_status.id


@pytest.mark.asyncio
async def test_allowed_statuses_endpoint(
    client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    new_status: IssueStatus,
    in_progress_status: IssueStatus,
    rejected_status: IssueStatus,
    priority: IssuePriority,
    dev_user: User,
    dev_member: Member,
    workflow_rules: list[WorkflowTransition],
) -> None:
    """GET /issues/{ref}/allowed-statuses returns correct list."""
    token = await _login(client, dev_user.login)
    issue = await _create_issue(client, token, project.key, tracker.id, new_status.id, priority.id)

    resp = await client.get(
        f"/api/v1/issues/{issue['key']}/allowed-statuses/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # New -> In Progress, New -> Rejected
    assert sorted(data["allowed_status_ids"]) == sorted([in_progress_status.id, rejected_status.id])


@pytest.mark.pro
@pytest.mark.asyncio
async def test_required_field_on_transition(
    client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    new_status: IssueStatus,
    in_progress_status: IssueStatus,
    priority: IssuePriority,
    developer_role: Role,
    dev_user: User,
    dev_member: Member,
    workflow_rules: list[WorkflowTransition],
) -> None:
    """Transition to In Progress requires assigned_to_id, omit -> 422."""
    # Add field rule: assigned_to_id required when In Progress
    rule = WorkflowFieldRule(
        tracker_id=tracker.id,
        role_id=developer_role.id,
        status_id=in_progress_status.id,
        field_name="assigned_to_id",
        rule="required",
    )
    db_session.add(rule)
    await db_session.commit()

    token = await _login(client, dev_user.login)
    issue = await _create_issue(client, token, project.key, tracker.id, new_status.id, priority.id)

    # Try transition without assigned_to_id
    resp = await client.patch(
        f"/api/v1/issues/{issue['key']}/",
        json={
            "status_id": in_progress_status.id,
            "lock_version": issue["lock_version"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    error = body["errors"][0]
    assert error["code"] == "workflow_field_required"
    assert error["field"] == "assigned_to_id"


@pytest.mark.pro
@pytest.mark.asyncio
async def test_readonly_field_on_transition(
    client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    new_status: IssueStatus,
    in_progress_status: IssueStatus,
    resolved_status: IssueStatus,
    closed_status: IssueStatus,
    priority: IssuePriority,
    developer_role: Role,
    dev_user: User,
    dev_member: Member,
    workflow_rules: list[WorkflowTransition],
) -> None:
    """Subject is readonly when Closed, attempt change -> 422."""
    # Add rule: subject readonly when Resolved
    rule = WorkflowFieldRule(
        tracker_id=tracker.id,
        role_id=developer_role.id,
        status_id=resolved_status.id,
        field_name="subject",
        rule="readonly",
    )
    db_session.add(rule)
    await db_session.commit()

    token = await _login(client, dev_user.login)
    issue = await _create_issue(client, token, project.key, tracker.id, new_status.id, priority.id)

    # Move to In Progress first
    resp = await client.patch(
        f"/api/v1/issues/{issue['key']}/",
        json={"status_id": in_progress_status.id, "lock_version": issue["lock_version"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    lv = resp.json()["lock_version"]

    # Move to Resolved + try to change subject
    resp = await client.patch(
        f"/api/v1/issues/{issue['key']}/",
        json={
            "status_id": resolved_status.id,
            "subject": "Changed subject",
            "lock_version": lv,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    error = body["errors"][0]
    assert error["code"] == "workflow_field_readonly"
    assert error["field"] == "subject"
