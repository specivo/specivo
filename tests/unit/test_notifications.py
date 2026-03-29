"""Unit tests for notification service logic.

Tests core notification rules:
- No self-notification (actor never notified for own action)
- Dedup (user is both watcher and assignee -> one email)
- Default preference is enabled (no preference record -> notify)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from specivo.services.notification_service import NotificationService, _NotifPrefs


def _make_user(user_id: int, email: str = "") -> MagicMock:
    """Build a mock User with the given id and email."""
    user = MagicMock()
    user.id = user_id
    user.email = email or f"user{user_id}@example.com"
    user.display_name = f"User {user_id}"
    return user


def _make_issue(
    issue_id: int = 1,
    project_id: int = 1,
    assigned_to_id: int | None = None,
    display_key: str = "TEST-1",
    subject: str = "Test issue",
) -> MagicMock:
    """Build a mock Issue."""
    issue = MagicMock()
    issue.id = issue_id
    issue.project_id = project_id
    issue.assigned_to_id = assigned_to_id
    issue.display_key = display_key
    issue.subject = subject
    return issue


@pytest.mark.asyncio
async def test_no_self_notification() -> None:
    """Actor (who made the change) must NOT receive a notification for own action."""
    service = NotificationService()
    session = AsyncMock()

    actor = _make_user(1, "actor@example.com")
    issue = _make_issue(assigned_to_id=1)  # assigned to actor

    # _get_prefs returns defaults (both enabled)
    with (
        patch.object(service, "_get_prefs", return_value=_NotifPrefs()),
        patch.object(service, "_queue_email") as mock_queue,
        patch.object(service, "create_notification") as mock_create_notif,
    ):
        await service.notify_assignment(
            session=session,
            issue=issue,
            old_assignee_id=None,
            new_assignee_id=actor.id,
            actor=actor,
        )
        # Actor assigned to self -> no notification
        mock_queue.assert_not_called()
        mock_create_notif.assert_not_called()


@pytest.mark.asyncio
async def test_notification_dedup() -> None:
    """A user who is both watcher and assignee receives only one email."""
    service = NotificationService()
    session = AsyncMock()

    actor = _make_user(1, "actor@example.com")
    watcher_and_assignee = _make_user(2, "both@example.com")
    issue = _make_issue(assigned_to_id=2)

    # list_watchers returns [watcher_and_assignee] -- same user as assignee
    watcher_svc = AsyncMock()
    watcher_svc.list_watchers.return_value = [watcher_and_assignee]

    # Assignee lookup
    session.execute = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = watcher_and_assignee
    session.execute.return_value = mock_result

    with (
        patch.object(service, "_watcher_service", watcher_svc),
        patch.object(service, "_get_prefs", return_value=_NotifPrefs()),
        patch.object(service, "_queue_email") as mock_queue,
        patch.object(service, "create_notification"),
    ):
        await service.notify_comment(
            session=session,
            issue=issue,
            journal=MagicMock(),
            actor=actor,
        )
        # Should only be called once for user 2, not twice
        emails_sent_to = [call.args[0] for call in mock_queue.call_args_list]
        assert emails_sent_to.count("both@example.com") == 1


@pytest.mark.asyncio
async def test_default_preference_is_enabled() -> None:
    """When no NotificationPreference record exists, _get_prefs returns both enabled."""
    service = NotificationService()
    session = AsyncMock()

    # No preference record found
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)

    prefs = await service._get_prefs(session, user_id=99, project_id=1, event_type="assignment")
    assert prefs.email_enabled is True
    assert prefs.in_app_enabled is True
