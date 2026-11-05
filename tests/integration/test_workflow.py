"""Integration tests for workflow engine — transition validation on issue updates.

Covers:
- Valid transitions succeed
- Invalid transitions return 422 with workflow_transition_denied
- Admin bypasses workflow validation
- No rules = allow any transition (backward compat)
- GET /issues/{ref}/allowed-statuses endpoint
- Required field validation on transition
- Readonly field validation on transition
- Field rules on every update, judged against what the request changes
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
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def in_progress_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="In Progress", position=2, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def resolved_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="Resolved", position=3, category="backlog")
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
async def rejected_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="Rejected", position=6, category="closed")
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
    # Note: NO workflow_rules fixture — table must be empty
) -> None:
    """When workflow_transitions is empty, any status change works (backward compat)."""
    # Clear any seed workflow rules so the "no rules" premise holds
    from sqlalchemy import text

    await db_session.execute(text("DELETE FROM workflow_transitions"))
    await db_session.commit()

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


# ---------------------------------------------------------------------------
# Field rules judge the change
# ---------------------------------------------------------------------------


def _rule(tracker: Tracker, role: Role, status: IssueStatus, field_name: str, rule: str) -> WorkflowFieldRule:
    return WorkflowFieldRule(
        tracker_id=tracker.id, role_id=role.id, status_id=status.id, field_name=field_name, rule=rule
    )


async def _patch(client: AsyncClient, token: str, key: str, **fields) -> object:
    resp = await client.get(f"/api/v1/issues/{key}/", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    return await client.patch(
        f"/api/v1/issues/{key}/",
        json={**fields, "lock_version": resp.json()["lock_version"]},
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest_asyncio.fixture
async def version(db_session: AsyncSession, project: Project):
    from tests.factories.version import VersionFactory

    v = VersionFactory.build(project_id=project.id, name="wf-1.0")
    db_session.add(v)
    await db_session.commit()
    await db_session.refresh(v)
    return v


@pytest.mark.asyncio
async def test_readonly_blocks_a_change_while_in_that_status(
    client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    new_status: IssueStatus,
    priority: IssuePriority,
    developer_role: Role,
    dev_user: User,
    dev_member: Member,
    workflow_rules: list[WorkflowTransition],
) -> None:
    """A read-only field is protected while the issue sits in the status, not only on the way in."""
    db_session.add(_rule(tracker, developer_role, new_status, "subject", "readonly"))
    await db_session.commit()
    token = await _login(client, dev_user.login)
    issue = await _create_issue(client, token, project.key, tracker.id, new_status.id, priority.id)

    resp = await _patch(client, token, issue["key"], subject="Changed while New")
    assert resp.status_code == 422, resp.text
    error = resp.json()["errors"][0]
    assert (error["code"], error["field"]) == ("workflow_field_readonly", "subject")
    # Other fields stay editable.
    resp = await _patch(client, token, issue["key"], description="More detail")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_readonly_allows_resending_the_same_value(
    client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    new_status: IssueStatus,
    in_progress_status: IssueStatus,
    resolved_status: IssueStatus,
    priority: IssuePriority,
    developer_role: Role,
    dev_user: User,
    dev_member: Member,
    workflow_rules: list[WorkflowTransition],
) -> None:
    """The edit form sends every field: an unchanged read-only value is not a change."""
    db_session.add(_rule(tracker, developer_role, resolved_status, "subject", "readonly"))
    db_session.add(_rule(tracker, developer_role, resolved_status, "done_ratio", "readonly"))
    await db_session.commit()
    token = await _login(client, dev_user.login)
    issue = await _create_issue(client, token, project.key, tracker.id, new_status.id, priority.id)
    resp = await _patch(client, token, issue["key"], status_id=in_progress_status.id)
    assert resp.status_code == 200, resp.text

    resp = await _patch(
        client, token, issue["key"], status_id=resolved_status.id, subject=issue["subject"], done_ratio=0
    )
    assert resp.status_code == 200, resp.text
    resp = await _patch(client, token, issue["key"], subject=issue["subject"], description="Notes")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_required_refuses_an_explicit_clear(
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
    version,
) -> None:
    """Clearing a required field is refused, on a transition and while in the status."""
    db_session.add(_rule(tracker, developer_role, in_progress_status, "fixed_version_id", "required"))
    db_session.add(_rule(tracker, developer_role, new_status, "fixed_version_id", "required"))
    await db_session.commit()
    token = await _login(client, dev_user.login)
    issue = await _create_issue(client, token, project.key, tracker.id, new_status.id, priority.id)

    # Old issues without the field stay editable: only clearing it is refused.
    resp = await _patch(client, token, issue["key"], subject="Edited without a version")
    assert resp.status_code == 200, resp.text
    resp = await _patch(client, token, issue["key"], fixed_version_id=version.id)
    assert resp.status_code == 200, resp.text

    resp = await _patch(client, token, issue["key"], fixed_version_id=None)
    assert resp.status_code == 422, resp.text
    assert resp.json()["errors"][0]["code"] == "workflow_field_required"

    resp = await _patch(client, token, issue["key"], status_id=in_progress_status.id, fixed_version_id=None)
    assert resp.status_code == 422, resp.text
    error = resp.json()["errors"][0]
    assert (error["code"], error["field"]) == ("workflow_field_required", "fixed_version_id")

    resp = await _patch(client, token, issue["key"], status_id=in_progress_status.id)
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_unknown_field_name_in_rule_is_refused(
    admin_client: AsyncClient,
    tracker: Tracker,
    new_status: IssueStatus,
    developer_role: Role,
) -> None:
    body = {"tracker_id": tracker.id, "role_id": developer_role.id, "status_id": new_status.id, "rule": "required"}
    resp = await admin_client.post("/api/v1/admin/workflows/field-rules/", json={**body, "field_name": "asignee"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["errors"][0]["field"] == "field_name"

    resp = await admin_client.post("/api/v1/admin/workflows/field-rules/", json={**body, "field_name": "due_date"})
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_admin_bypasses_field_rules(
    client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    new_status: IssueStatus,
    in_progress_status: IssueStatus,
    priority: IssuePriority,
    developer_role: Role,
    admin_user: User,
    workflow_rules: list[WorkflowTransition],
) -> None:
    db_session.add(_rule(tracker, developer_role, in_progress_status, "assigned_to_id", "required"))
    db_session.add(_rule(tracker, developer_role, new_status, "subject", "readonly"))
    member = Member(user_id=admin_user.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=developer_role.id))
    await db_session.commit()

    token = await _login(client, admin_user.login)
    issue = await _create_issue(client, token, project.key, tracker.id, new_status.id, priority.id)
    resp = await _patch(client, token, issue["key"], subject="Admin edit")
    assert resp.status_code == 200, resp.text
    resp = await _patch(client, token, issue["key"], status_id=in_progress_status.id)
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_field_rules_add_no_query_when_no_rules_exist(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    new_status: IssueStatus,
    in_progress_status: IssueStatus,
    priority: IssuePriority,
    dev_user: User,
    dev_member: Member,
) -> None:
    """With no rules for the tracker, checking them costs at most one small query."""
    from sqlalchemy import event

    from specivo.schemas.issue import IssueCreate, IssueUpdate
    from specivo.services.issue_service import IssueService
    from specivo.services.workflow_service import WorkflowService

    issue = await IssueService().create(
        db_session,
        project,
        IssueCreate(project_key=project.key, tracker_id=tracker.id, subject="No rules"),
        dev_user,
    )
    await db_session.commit()
    statements: list[str] = []

    def _record(conn, cursor, statement, *args) -> None:  # noqa: ANN001
        # The test's own transaction may open a savepoint; that is not the check's.
        if not statement.startswith(("SAVEPOINT", "RELEASE SAVEPOINT")):
            statements.append(statement)

    data = IssueUpdate(subject="Renamed", status_id=in_progress_status.id, lock_version=issue.lock_version)
    engine = db_session.bind.sync_engine  # type: ignore[union-attr]
    event.listen(engine, "before_cursor_execute", _record)
    try:
        await WorkflowService().validate_field_rules(db_session, issue, data, dev_user, in_progress_status.id)
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    assert len(statements) <= 1, statements


async def _bulk(client: AsyncClient, token: str, issue_id: int, **updates) -> dict:
    resp = await client.post(
        "/api/v1/issues/bulk-update/",
        json={"issue_ids": [issue_id], "updates": updates},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_bulk_update_cannot_change_a_read_only_version(
    client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    new_status: IssueStatus,
    priority: IssuePriority,
    developer_role: Role,
    dev_user: User,
    dev_member: Member,
    workflow_rules: list[WorkflowTransition],
    version,
) -> None:
    db_session.add(_rule(tracker, developer_role, new_status, "fixed_version_id", "readonly"))
    await db_session.commit()
    token = await _login(client, dev_user.login)
    issue = await _create_issue(client, token, project.key, tracker.id, new_status.id, priority.id)

    result = await _bulk(client, token, issue["id"], fixed_version_id=version.id)

    assert result["failed"][0]["error"]["code"] == "workflow_field_readonly"
    assert result["succeeded"] == []


@pytest.mark.asyncio
async def test_bulk_update_setting_a_required_version_with_the_status_succeeds(
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
    version,
) -> None:
    db_session.add(_rule(tracker, developer_role, in_progress_status, "fixed_version_id", "required"))
    await db_session.commit()
    token = await _login(client, dev_user.login)
    issue = await _create_issue(client, token, project.key, tracker.id, new_status.id, priority.id)

    result = await _bulk(client, token, issue["id"], status_id=in_progress_status.id, fixed_version_id=version.id)

    assert result["failed"] == []
    resp = await client.get(f"/api/v1/issues/{issue['key']}/", headers={"Authorization": f"Bearer {token}"})
    assert resp.json()["fixed_version_id"] == version.id


@pytest.mark.asyncio
async def test_read_only_rule_of_the_current_tracker_stops_a_tracker_change(
    client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    new_status: IssueStatus,
    priority: IssuePriority,
    developer_role: Role,
    dev_user: User,
    dev_member: Member,
    workflow_rules: list[WorkflowTransition],
) -> None:
    other = TrackerFactory.build(default_status_id=new_status.id)
    db_session.add(other)
    await db_session.flush()
    db_session.add(_rule(tracker, developer_role, new_status, "tracker_id", "readonly"))
    await db_session.commit()
    token = await _login(client, dev_user.login)
    issue = await _create_issue(client, token, project.key, tracker.id, new_status.id, priority.id)

    resp = await _patch(client, token, issue["key"], tracker_id=other.id)

    assert resp.status_code == 422, resp.text
    assert resp.json()["errors"][0]["code"] == "workflow_field_readonly"


@pytest.mark.asyncio
async def test_transition_by_a_user_with_no_project_role_is_refused_not_a_crash(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    new_status: IssueStatus,
    in_progress_status: IssueStatus,
    dev_user: User,
    workflow_rules: list[WorkflowTransition],
) -> None:
    from types import SimpleNamespace

    from specivo.core.exceptions import AppError
    from specivo.services.workflow_service import WorkflowService

    issue = SimpleNamespace(project_id=project.id, tracker_id=tracker.id, status_id=new_status.id)
    with pytest.raises(AppError) as refused:
        await WorkflowService().validate_transition(db_session, issue, in_progress_status.id, dev_user)
    assert refused.value.code == "workflow_transition_denied"
