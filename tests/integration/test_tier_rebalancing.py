"""Integration tests for v0.15.0 tier rebalancing.

TDD RED phase -- these tests describe the DESIRED behaviour after
the rebalancing is implemented.  They run in core-only mode
(INSTALLED_PLUGINS=[]) and verify that features previously gated
behind Pro now work without any plugin.

Issue references:
- -- Features moved from Pro to CE (threaded comments, reactions,
          mentions, bulk, saved filters)
- -- In-app notifications moved from Pro to CE
- -- Metadata validation / workflow field rules moved to Enterprise
- -- API key limit raised from 5 to 20

Expected failures (RED):
- Threaded comments: reply_to_id is silently dropped, resolve_thread raises
- Reactions / saved-filters endpoints: 404 (routes not mounted in core)
- In-app notification creation: not created without pro plugin
- API key limit: 6th key fails (limit is still 5)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.notification import Notification
from specivo.models.project import Project
from specivo.models.user import User
from specivo.models.workflow import WorkflowFieldRule, WorkflowTransition
from specivo.services.api_key_service import ApiKeyService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login",
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
        f"/api/v1/projects/{project_key}/issues",
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
    proj = ProjectFactory.build(key="TRB", identifier="tier-rebalance-test")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="trb_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


@pytest_asyncio.fixture
async def second_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="trb_user2", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def second_token(second_user: User, client: AsyncClient) -> str:
    return await _login(client, second_user.login)


# ---------------------------------------------------------------------------
# Features now in core (no plugin needed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_threaded_comments_work_without_pro(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """POST comment with reply_to_id succeeds in core-only mode.

    Currently reply_to_id is silently dropped when threaded_comments is
    not registered.  After rebalancing, threaded_comments is a core
    feature and reply_to_id must be preserved.
    """
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
        f"/api/v1/issues/{issue_key}/journals",
        json={"notes": "Parent comment"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    parent_id = resp.json()["id"]

    # Create reply -- reply_to_id must be preserved (not silently dropped)
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals",
        json={"notes": "Reply to parent", "reply_to_id": parent_id},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    reply_data = resp.json()

    # The reply must reference the parent
    assert reply_data.get("reply_to_id") == parent_id, (
        "reply_to_id should be preserved in core-only mode after tier rebalancing"
    )


@pytest.mark.asyncio
async def test_resolve_thread_works_without_pro(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """resolve_thread must succeed in core-only mode after rebalancing.

    Currently raises RuntimeError because threaded_comments feature is not
    registered.
    """
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
        f"/api/v1/issues/{issue_key}/journals",
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
        summary="Resolved in core-only mode",
    )
    assert resolved.is_resolved is True
    assert resolved.resolved_summary == "Resolved in core-only mode"


@pytest.mark.asyncio
async def test_reactions_endpoint_in_core(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """POST /reactions must return non-404 in core-only mode.

    Currently the reactions router is only mounted by ProPlugin, so this
    returns 404 without the plugin.
    """
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
        f"/api/v1/issues/{issue_key}/journals",
        json={"notes": "React to this"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    journal_id = resp.json()["id"]

    # POST a reaction -- must NOT be 404
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/{journal_id}/reactions",
        json={"emoji": "+1"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code != 404, (
        f"Reactions endpoint returned 404 in core-only mode; "
        f"expected it to be available after tier rebalancing (got {resp.status_code})"
    )


@pytest.mark.asyncio
async def test_bulk_update_endpoint_in_core(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """POST /issues/bulk-update must return non-404 in core-only mode."""
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
        "/api/v1/issues/bulk-update",
        json={"issue_ids": [issue["id"]], "updates": {"subject": "Bulk updated"}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code != 404, f"Bulk update endpoint returned 404; expected non-404 (got {resp.status_code})"


@pytest.mark.asyncio
async def test_saved_filters_endpoint_in_core(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
) -> None:
    """GET /projects/{key}/saved-filters must return non-404 in core-only mode.

    Currently saved-filters router is mounted by ProPlugin only.
    """
    resp = await client.get(
        f"/api/v1/projects/{project.key}/saved-filters",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code != 404, (
        f"Saved filters endpoint returned 404 in core-only mode; "
        f"expected it to be available after tier rebalancing (got {resp.status_code})"
    )


@pytest.mark.asyncio
async def test_mentions_work_without_pro(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    second_user: User,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Comment with @mention creates a notification in core-only mode.

    After rebalancing, the mention service must create Notification records
    without requiring the pro plugin.
    """
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
            f"/api/v1/issues/{issue_key}/journals",
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
    assert notif is not None, "Expected an in-app notification for @mention in core-only mode"


# ---------------------------------------------------------------------------
# In-app notifications in core
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_app_notifications_created_without_pro(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    second_user: User,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """After issue assignment, a Notification record must exist in core-only mode.

    Currently in-app notifications may only be created when the pro plugin
    provides the feature.  After rebalancing, they are a core feature.
    """
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
            f"/api/v1/issues/{issue_key}",
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
    assert notif is not None, "Expected an in-app Notification record for assignment in core-only mode"


@pytest.mark.asyncio
async def test_notifications_endpoint_in_core(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
) -> None:
    """GET /notifications must return non-404 in core-only mode."""
    resp = await client.get(
        "/api/v1/notifications",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code != 404, f"Notifications endpoint returned 404 in core-only mode (got {resp.status_code})"
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_notification_preferences_in_core(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
) -> None:
    """Notification preferences must be accessible in core-only mode."""
    resp = await client.get(
        "/api/v1/notification-preferences",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, f"Notification preferences returned {resp.status_code} in core-only mode"


@pytest.mark.asyncio
async def test_notification_bell_not_feature_gated(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
) -> None:
    """The notification bell section in the header template must render
    without the in_app_notifications feature being registered by a plugin.

    Currently the header template uses:
        {% if has_feature("in_app_notifications") %}
    which means the bell is hidden in core-only mode.  After rebalancing,
    the bell must be visible.
    """
    # Access any web page that renders the header
    resp = await client.get(
        "/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    # The page should load (may redirect to login or dashboard, either is fine)
    if resp.status_code in (200, 302, 307):
        # If we got HTML, check for the notification element
        if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
            body = resp.text
            # After rebalancing, the notification section must be rendered
            # regardless of plugin presence.  Look for the notification-related
            # CSS class or element that the template conditionally renders.
            assert "notification" in body.lower(), (
                "Notification bell section not found in header; "
                "it should render in core-only mode after tier rebalancing"
            )


# ---------------------------------------------------------------------------
# Metadata/field rules moved to enterprise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_validation_requires_enterprise(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Without enterprise plugin, metadata is stored but NOT validated.

    Even if a metadata schema exists, core-only should accept any metadata
    and store it as-is (no 422 from schema validation).
    """
    # Create a metadata schema that would normally enforce validation
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

    # Create issue with metadata that VIOLATES the schema (missing "severity")
    # In core-only mode: should succeed (200/201), metadata stored as-is
    # With enterprise: would return 422 for missing required field
    body: dict = {
        "project_key": project.key,
        "tracker_id": tracker.id,
        "subject": "Metadata validation test",
        "status_id": open_status.id,
        "priority_id": priority.id,
        "metadata": {"notes": "no severity field provided"},
    }
    resp = await client.post(
        f"/api/v1/projects/{project.key}/issues",
        json=body,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    # Core-only: must NOT reject with 422 for metadata validation
    assert resp.status_code == 201, (
        f"Expected 201 (metadata stored without validation in core-only), got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_workflow_field_rules_require_enterprise(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    in_progress_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Without enterprise, field rules are not enforced (200 not 422).

    Currently workflow_field_rules feature gate is checked and returns
    early (no-op) when the feature is not registered.  This test confirms
    that behaviour is preserved -- field rules only take effect with the
    enterprise plugin.
    """
    from specivo.models.member import Member, MemberRole
    from specivo.models.role import Role

    # Set up role and membership
    role = Role(name="Developer", position=1)
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)

    member = Member(
        user_id=admin_user.id,
        project_id=project.id,
    )
    db_session.add(member)
    await db_session.flush()

    member_role = MemberRole(
        member_id=member.id,
        role_id=role.id,
    )
    db_session.add(member_role)

    # Set up workflow transition: New -> In Progress
    transition = WorkflowTransition(
        tracker_id=tracker.id,
        role_id=role.id,
        old_status_id=open_status.id,
        new_status_id=in_progress_status.id,
    )
    db_session.add(transition)

    # Add a field rule: assigned_to_id required when transitioning to In Progress
    rule = WorkflowFieldRule(
        tracker_id=tracker.id,
        role_id=role.id,
        status_id=in_progress_status.id,
        field_name="assigned_to_id",
        rule="required",
    )
    db_session.add(rule)
    await db_session.commit()

    issue = await _create_issue(
        client,
        admin_token,
        project.key,
        tracker.id,
        open_status.id,
        priority.id,
        subject="Field rules test",
    )

    # Transition to In Progress WITHOUT assigned_to_id
    # Without enterprise: field rule is ignored -> 200
    # With enterprise: field rule enforced -> 422
    resp = await client.patch(
        f"/api/v1/issues/{issue['key']}",
        json={
            "status_id": in_progress_status.id,
            "lock_version": issue["lock_version"],
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, (
        f"Expected 200 (field rules not enforced without enterprise), got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# API key limit raised to 20
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_key_limit_is_20_in_core(
    db_session: AsyncSession,
    admin_user: User,
) -> None:
    """Core-only mode must allow creating 20 API keys (up from 5).

    The 21st key must fail with api_key_limit error.
    Currently _MAX_KEYS_FREE is 5, so key #6 already fails.
    """
    service = ApiKeyService()

    # Create 20 keys -- all must succeed
    for i in range(20):
        _key, _raw = await service.create_key(
            session=db_session,
            user_id=admin_user.id,
            name=f"key-{i + 1:02d}",
        )
    await db_session.flush()

    # 21st key must fail
    with pytest.raises(AppError) as exc_info:
        await service.create_key(
            session=db_session,
            user_id=admin_user.id,
            name="key-21-should-fail",
        )
    assert exc_info.value.code == "api_key_limit"
    assert "20" in str(exc_info.value.message), "Error message should reference the new limit of 20"
