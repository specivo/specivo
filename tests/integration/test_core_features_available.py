"""Integration tests: collaboration and validation features work with no plugin.

Threaded comments, reactions, mentions, in-app notifications, saved filters,
bulk updates, metadata validation and workflow field rules are all part of
Specivo itself. These tests run without plugins (``INSTALLED_PLUGINS=[]``).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.notification import Notification
from specivo.models.project import Project
from specivo.models.user import User
from specivo.models.workflow import WorkflowFieldRule, WorkflowTransition
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": login, "password": TEST_PASSWORD},
    )
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
    assigned_to_id: int | None = None,
) -> dict:
    body: dict = {
        "project_key": project_key,
        "tracker_id": tracker_id,
        "subject": subject,
        "status_id": status_id,
        "priority_id": priority_id,
    }
    if assigned_to_id is not None:
        body["assigned_to_id"] = assigned_to_id
    resp = await client.post(
        f"/api/v1/projects/{project_key}/issues/",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def open_status(db_session: AsyncSession) -> IssueStatus:
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
async def tracker(db_session: AsyncSession, open_status: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Task", default_status_id=open_status.id)
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
    proj = ProjectFactory.build(key="CFA", identifier="core-features-test")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="cfa_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


@pytest_asyncio.fixture
async def second_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="cfa_user2", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def second_user_member(db_session: AsyncSession, second_user: User, project: Project) -> User:
    """``second_user`` as a member who can see the project's issues, so notifications reach them."""
    from specivo.models.member import Member, MemberRole
    from specivo.models.role import Role

    role = Role(name="Cfa Viewer", permissions=["view_issues"], builtin=0, issues_visibility="default")
    member = Member(user_id=second_user.id, project_id=project.id)
    db_session.add_all([role, member])
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await db_session.commit()
    return second_user


@pytest_asyncio.fixture
async def second_token(second_user: User, client: AsyncClient) -> str:
    return await _login(client, second_user.login)


# ---------------------------------------------------------------------------
# Comments, reactions, saved filters and bulk updates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_threaded_comment_replies_keep_their_parent(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """A comment posted with reply_to_id keeps it."""
    issue = await _create_issue(
        client,
        admin_token,
        project.key,
        tracker.id,
        open_status.id,
        priority.id,
        subject="Thread test",
    )
    issue_key = issue["key"]

    # Create parent comment
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "Parent comment"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    parent_id = resp.json()["id"]

    # Create reply -- reply_to_id must be preserved
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "Reply to parent", "reply_to_id": parent_id},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    reply_data = resp.json()

    # The reply must reference the parent
    assert reply_data.get("reply_to_id") == parent_id


@pytest.mark.asyncio
async def test_a_thread_can_be_resolved(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """resolve_thread marks a discussion resolved with its summary."""
    issue = await _create_issue(
        client,
        admin_token,
        project.key,
        tracker.id,
        open_status.id,
        priority.id,
        subject="Resolve thread test",
    )
    issue_key = issue["key"]

    # Create a comment to resolve
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "Discussion to resolve"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    journal_id = resp.json()["id"]

    # Resolve the thread via the service layer
    from specivo.services.journal_service import JournalService

    svc = JournalService()
    resolved = await svc.resolve_thread(
        session=db_session,
        journal_id=journal_id,
        issue_id=issue["id"],
        user=admin_user,
        summary="Resolved",
    )
    assert resolved.is_resolved is True
    assert resolved.resolved_summary == "Resolved"


@pytest.mark.asyncio
async def test_reactions_endpoint_is_served(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """POST /reactions is served."""
    issue = await _create_issue(
        client,
        admin_token,
        project.key,
        tracker.id,
        open_status.id,
        priority.id,
        subject="Reactions test",
    )
    issue_key = issue["key"]

    # Create a comment to react to
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "React to this"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    journal_id = resp.json()["id"]

    # POST a reaction -- must NOT be 404
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/{journal_id}/reactions/",
        json={"emoji": "+1"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code != 404, resp.text


@pytest.mark.asyncio
async def test_bulk_update_endpoint_is_served(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """POST /issues/bulk-update is served."""
    issue = await _create_issue(
        client,
        admin_token,
        project.key,
        tracker.id,
        open_status.id,
        priority.id,
        subject="Bulk test",
    )

    resp = await client.post(
        "/api/v1/issues/bulk-update/",
        json={"issue_ids": [issue["id"]], "updates": {"subject": "Bulk updated"}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code != 404, resp.text


@pytest.mark.asyncio
async def test_saved_filters_endpoint_is_served(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
) -> None:
    """GET /projects/{key}/saved-filters/ is served."""
    resp = await client.get(
        f"/api/v1/projects/{project.key}/saved-filters/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code != 404, resp.text


@pytest.mark.asyncio
async def test_a_mention_notifies_the_mentioned_user(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    second_user: User,
    second_user_member: User,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """A comment with an @mention creates an in-app notification."""
    issue = await _create_issue(
        client,
        admin_token,
        project.key,
        tracker.id,
        open_status.id,
        priority.id,
        subject="Mention test",
    )
    issue_key = issue["key"]

    # Post a comment mentioning second_user
    with patch("specivo.tasks.notifications.send_notification_email.delay"):
        resp = await client.post(
            f"/api/v1/issues/{issue_key}/journals/",
            json={"notes": f"Hey @{second_user.login} please review"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert resp.status_code == 201, resp.text

    # Verify an in-app notification was created for the mentioned user
    result = await db_session.execute(
        select(Notification).where(
            Notification.user_id == second_user.id,
            Notification.event_type == "mention",
        )
    )
    notif = result.scalar_one_or_none()
    assert notif is not None, "Expected an in-app notification for the @mention"


# ---------------------------------------------------------------------------
# In-app notifications
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assignment_creates_an_in_app_notification(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    second_user: User,
    second_user_member: User,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Assigning an issue creates an in-app Notification for the assignee."""
    issue = await _create_issue(
        client,
        admin_token,
        project.key,
        tracker.id,
        open_status.id,
        priority.id,
        subject="In-app notification test",
    )
    issue_key = issue["key"]

    with patch("specivo.tasks.notifications.send_notification_email.delay"):
        resp = await client.patch(
            f"/api/v1/issues/{issue_key}/",
            json={
                "assigned_to_id": second_user.id,
                "lock_version": issue["lock_version"],
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert resp.status_code == 200, resp.text

    # Check that an in-app Notification record was created
    result = await db_session.execute(
        select(Notification).where(
            Notification.user_id == second_user.id,
            Notification.event_type == "assignment",
        )
    )
    notif = result.scalar_one_or_none()
    assert notif is not None, "Expected an in-app Notification record for the assignment"


@pytest.mark.asyncio
async def test_notifications_endpoint_is_served(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
) -> None:
    """GET /notifications/ is served."""
    resp = await client.get(
        "/api/v1/notifications/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_notification_preferences_are_served(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
) -> None:
    """Notification preferences are served."""
    resp = await client.get(
        "/api/v1/notification-preferences/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_notification_bell_renders(admin_client: AsyncClient) -> None:
    """The header renders the notification bell and its dropdown."""
    resp = await admin_client.get("/", cookies={"access_token": admin_client.state.token})
    assert resp.status_code == 200, resp.text
    assert "notif-dropdown" in resp.text


# ---------------------------------------------------------------------------
# Metadata validation and workflow field rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_validation_applies_without_a_plugin(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Metadata is validated against the project's schemas, admins included."""
    from specivo.models.metadata_schema import MetadataSchema

    schema = MetadataSchema(
        name="bug_details",
        project_id=project.id,
        tracker_id=tracker.id,
        schema_definition={
            "type": "object",
            "required": ["severity"],
            "properties": {
                "severity": {"type": "string", "enum": ["low", "medium", "high"]},
            },
        },
    )
    db_session.add(schema)
    await db_session.commit()

    body: dict = {
        "project_key": project.key,
        "tracker_id": tracker.id,
        "subject": "Metadata validation test",
        "status_id": open_status.id,
        "priority_id": priority.id,
        "metadata": {"notes": "no severity field provided"},
    }
    resp = await client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json=body,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422, resp.text
    error = resp.json()["errors"][0]
    assert (error["code"], error["field"]) == ("validation_error", "metadata.severity")


@pytest.mark.asyncio
async def test_workflow_field_rules_apply_without_a_plugin(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    in_progress_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Field rules are enforced for a non-admin member."""
    from specivo.models.member import Member, MemberRole
    from specivo.models.role import Role

    role = Role(
        name="CoreFieldRules",
        position=41,
        builtin=0,
        permissions=["view_issues", "add_issues", "edit_issues"],
        issues_visibility="default",
        settings={},
    )
    member_user = UserFactory.build(login="core_field_rules", status="active")
    db_session.add_all([role, member_user])
    await db_session.flush()
    member = Member(user_id=member_user.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    db_session.add(
        WorkflowTransition(
            tracker_id=tracker.id,
            role_id=role.id,
            old_status_id=open_status.id,
            new_status_id=in_progress_status.id,
        )
    )
    db_session.add(
        WorkflowFieldRule(
            tracker_id=tracker.id,
            role_id=role.id,
            status_id=in_progress_status.id,
            field_name="assigned_to_id",
            rule="required",
        )
    )
    await db_session.commit()

    token = await _login(client, member_user.login)
    issue = await _create_issue(
        client, token, project.key, tracker.id, open_status.id, priority.id, subject="Field rules test"
    )
    resp = await client.patch(
        f"/api/v1/issues/{issue['key']}/",
        json={"status_id": in_progress_status.id, "lock_version": issue["lock_version"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["errors"][0]["code"] == "workflow_field_required"
