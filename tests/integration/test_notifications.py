"""Integration tests for notifications.

Covers:
- Email queued on assignment change (via channel dispatch)
- Email queued on comment (to watchers)
- No email when preference disabled
- Notification preferences CRUD (channels JSONB)
- No self-notification on assignment
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.notification import NotificationPreference
from specivo.models.project import Project
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str, password: str = TEST_PASSWORD) -> str:
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
    proj = ProjectFactory.build(key="NTF", identifier="notification-test", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="notif_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


@pytest_asyncio.fixture
async def second_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="notif_user2", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def second_token(second_user: User, client: AsyncClient) -> str:
    return await _login(client, second_user.login)


# ---------------------------------------------------------------------------
# Tests: email queued on assignment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_queued_on_assignment(
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
    """Assigning an issue to another user queues an email via Celery."""
    # Create an unassigned issue
    issue_data = await _create_issue_via_api(
        client,
        admin_token,
        project.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Assignment notification test",
    )
    issue_key = issue_data["key"]

    with patch("specivo.tasks.notifications.send_notification_email.delay") as mock_delay:
        # Assign to second_user
        resp = await client.patch(
            f"/api/v1/issues/{issue_key}/",
            json={"assigned_to_id": second_user.id, "lock_version": issue_data["lock_version"]},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200, resp.text
        # Celery task should have been called with second_user's email
        assert mock_delay.called
        call_args = mock_delay.call_args
        assert second_user.email in call_args.args[0]


@pytest.mark.asyncio
async def test_email_queued_on_comment(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    second_user: User,
    second_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Adding a comment notifies watchers via Celery."""
    # Create issue (admin is auto-watched)
    issue_data = await _create_issue_via_api(
        client,
        admin_token,
        project.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Comment notification test",
    )
    issue_key = issue_data["key"]

    # second_user watches the issue
    await client.post(
        f"/api/v1/issues/{issue_key}/watchers/",
        headers={"Authorization": f"Bearer {second_token}"},
    )

    with patch("specivo.tasks.notifications.send_notification_email.delay") as mock_delay:
        # Admin adds a comment -> should notify second_user (watcher), not admin (actor)
        resp = await client.post(
            f"/api/v1/issues/{issue_key}/journals/",
            json={"notes": "This is a test comment"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 201, resp.text
        assert mock_delay.called
        # Gather all emails that were queued
        all_to_emails = [call.args[0] for call in mock_delay.call_args_list]
        assert second_user.email in all_to_emails
        # Actor (admin) should NOT be in the recipients
        assert admin_user.email not in all_to_emails


@pytest.mark.asyncio
async def test_no_email_when_preference_disabled(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    second_user: User,
    second_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """When a user disables email notification for an event type, no email is queued."""
    # Disable assignment email for second_user via channels JSONB
    pref = NotificationPreference(
        user_id=second_user.id,
        project_id=None,
        event_type="assignment",
        channels={"email": False, "in_app": True},
    )
    db_session.add(pref)
    await db_session.commit()

    # Create unassigned issue
    issue_data = await _create_issue_via_api(
        client,
        admin_token,
        project.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Preference disabled test",
    )
    issue_key = issue_data["key"]

    with patch("specivo.tasks.notifications.send_notification_email.delay") as mock_delay:
        # Assign to second_user
        resp = await client.patch(
            f"/api/v1/issues/{issue_key}/",
            json={"assigned_to_id": second_user.id, "lock_version": issue_data["lock_version"]},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200, resp.text
        # No email should have been queued for second_user
        all_to_emails = [call.args[0] for call in mock_delay.call_args_list]
        assert second_user.email not in all_to_emails


@pytest.mark.asyncio
async def test_notification_preferences_crud(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
) -> None:
    """GET and PATCH notification preferences (channels JSONB)."""
    # GET — initially empty (defaults apply)
    resp = await client.get(
        "/api/v1/notification-preferences/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []

    # PATCH — disable assignment email
    resp = await client.patch(
        "/api/v1/notification-preferences/",
        json={"event_type": "assignment", "channels": {"email": False, "in_app": True}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["event_type"] == "assignment"
    assert data["channels"]["email"] is False
    assert data["channels"]["in_app"] is True
    assert data["project_id"] is None

    # GET — now has one preference
    resp = await client.get(
        "/api/v1/notification-preferences/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    prefs = resp.json()
    assert len(prefs) == 1
    assert prefs[0]["event_type"] == "assignment"
    assert prefs[0]["channels"]["email"] is False

    # PATCH — re-enable email
    resp = await client.patch(
        "/api/v1/notification-preferences/",
        json={"event_type": "assignment", "channels": {"email": True, "in_app": True}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["channels"]["email"] is True


@pytest.mark.asyncio
async def test_assignment_no_self_notify(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Assigning an issue to oneself does NOT generate a notification."""
    issue_data = await _create_issue_via_api(
        client,
        admin_token,
        project.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Self-assign test",
    )
    issue_key = issue_data["key"]

    with patch("specivo.tasks.notifications.send_notification_email.delay") as mock_delay:
        # Admin assigns to self
        resp = await client.patch(
            f"/api/v1/issues/{issue_key}/",
            json={"assigned_to_id": admin_user.id, "lock_version": issue_data["lock_version"]},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200, resp.text
        # No notification email should be sent
        mock_delay.assert_not_called()
