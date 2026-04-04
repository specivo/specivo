"""Updated unit tests for NotificationService using the new dispatch architecture.

RED phase — these tests replace the behaviour tested in test_notifications.py
for the parts that change in Phase 2 & 3 of the notification channel
architecture plan. The original test_notifications.py is left untouched.

Changes from the original tests:
- _NotifPrefs is gone; _get_prefs now returns dict[str, bool]
- _queue_email is removed; dispatch goes through _dispatch_to_channels
- test_default_preference_is_enabled asserts prefs is a dict

Covers:
- test_no_self_notification_v2    — same rule, new dispatch path
- test_notification_dedup_v2      — same rule, new dispatch path
- test_default_preference_is_dict — _get_prefs returns {} not _NotifPrefs
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers (duplicated from test_notifications.py to keep files independent)
# ---------------------------------------------------------------------------


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
    project_key: str = "TEST",
) -> MagicMock:
    """Build a mock Issue."""
    issue = MagicMock()
    issue.id = issue_id
    issue.project_id = project_id
    issue.assigned_to_id = assigned_to_id
    issue.display_key = display_key
    issue.subject = subject
    issue.project = MagicMock()
    issue.project.key = project_key
    return issue


# ---------------------------------------------------------------------------
# No self-notification — new dispatch path
# ---------------------------------------------------------------------------


async def test_no_self_notification_v2() -> None:
    """Actor assigned to self must NOT receive a notification via new dispatch path."""
    from specivo.services.notification_service import NotificationService

    service = NotificationService()
    session = AsyncMock()

    actor = _make_user(1, "actor@example.com")
    issue = _make_issue(assigned_to_id=1)  # assigned to the actor

    with (
        patch.object(service, "_dispatch_to_channels", new_callable=AsyncMock) as mock_dispatch,
    ):
        await service.notify_assignment(
            session=session,
            issue=issue,
            old_assignee_id=None,
            new_assignee_id=actor.id,
            actor=actor,
        )
        # Actor assigned to self → dispatch must never be called
        mock_dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Notification dedup — new dispatch path
# ---------------------------------------------------------------------------


async def test_notification_dedup_v2() -> None:
    """A user who is both watcher and assignee receives dispatch only once."""
    from specivo.services.notification_service import NotificationService

    service = NotificationService()
    session = AsyncMock()

    actor = _make_user(1, "actor@example.com")
    watcher_and_assignee = _make_user(2, "both@example.com")
    issue = _make_issue(assigned_to_id=2)

    # list_watchers returns the same user as the assignee
    watcher_svc = AsyncMock()
    watcher_svc.list_watchers.return_value = [watcher_and_assignee]

    # Assignee DB lookup
    session.execute = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = watcher_and_assignee
    session.execute.return_value = mock_result

    with (
        patch.object(service, "_watcher_service", watcher_svc),
        patch.object(service, "_dispatch_to_channels", new_callable=AsyncMock) as mock_dispatch,
    ):
        await service.notify_comment(
            session=session,
            issue=issue,
            journal=MagicMock(),
            actor=actor,
        )
        # Count how many times user 2 was dispatched to
        calls_for_user_2 = [
            call for call in mock_dispatch.call_args_list if call.kwargs.get("user") is watcher_and_assignee
        ]
        assert len(calls_for_user_2) == 1


# ---------------------------------------------------------------------------
# Default preference is now a dict
# ---------------------------------------------------------------------------


async def test_default_preference_is_dict() -> None:
    """When no NotificationPreference row exists, _get_prefs returns a plain dict."""
    from specivo.services.notification_service import NotificationService

    service = NotificationService()
    session = AsyncMock()

    # Simulate no preference row found at either project or global level
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)

    prefs = await service._get_prefs(session, user_id=99, project_id=1, event_type="assignment")
    assert isinstance(prefs, dict)


async def test_default_preference_enables_all_channels() -> None:
    """The default empty prefs dict means all channels are considered enabled."""
    from specivo.services.notification_service import NotificationService

    service = NotificationService()
    session = AsyncMock()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)

    prefs = await service._get_prefs(session, user_id=99, project_id=1, event_type="assignment")
    assert service._is_channel_enabled(prefs, "email") is True
    assert service._is_channel_enabled(prefs, "in_app") is True


async def test_no_self_notification_with_none_new_assignee() -> None:
    """notify_assignment must return immediately when new_assignee_id is None."""
    from specivo.services.notification_service import NotificationService

    service = NotificationService()
    session = AsyncMock()
    actor = _make_user(1, "actor@example.com")
    issue = _make_issue(assigned_to_id=None)

    with patch.object(service, "_dispatch_to_channels", new_callable=AsyncMock) as mock_dispatch:
        await service.notify_assignment(
            session=session,
            issue=issue,
            old_assignee_id=None,
            new_assignee_id=None,
            actor=actor,
        )
        mock_dispatch.assert_not_called()
