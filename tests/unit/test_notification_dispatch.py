"""Unit tests for the new dispatch loop in NotificationService.

RED phase — these tests define the expected behaviour of the refactored
dispatch layer (Phase 2 & 3 of the notification channel architecture plan)
before any implementation exists.

Covers:
- _dispatch_to_channels calls create_notification when in_app enabled
- _dispatch_to_channels skips in_app when in_app disabled
- _dispatch_to_channels calls EmailChannel.dispatch when email enabled
- _dispatch_to_channels skips email when disabled in prefs
- _dispatch_to_channels skips channel when not configured for user
- _dispatch_to_channels catches per-channel errors without failing others
- _build_assignment_payload returns correct NotificationPayload
- _build_comment_payload includes comment_text from journal
- _is_channel_enabled returns True for missing key (default)
- _is_channel_enabled returns False for explicit False
- _is_channel_enabled returns True for explicit True
- _get_prefs returns dict from channels JSONB column
- _get_prefs returns empty dict (all defaults) when no preference record
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(user_id: int = 1, email: str = "user@example.com") -> MagicMock:
    user = MagicMock()
    user.id = user_id
    user.email = email
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
    issue = MagicMock()
    issue.id = issue_id
    issue.project_id = project_id
    issue.assigned_to_id = assigned_to_id
    issue.display_key = display_key
    issue.subject = subject
    # project relationship
    issue.project = MagicMock()
    issue.project.key = project_key
    return issue


def _make_journal(notes: str = "A comment") -> MagicMock:
    journal = MagicMock()
    journal.notes = notes
    return journal


def _make_channel(key: str, *, configured: bool = True, dispatch_raises: bool = False) -> MagicMock:
    """Build a mock NotificationChannel."""
    ch = MagicMock()
    type(ch).channel_key = PropertyMock(return_value=key)
    ch.is_configured_for_user.return_value = configured
    if dispatch_raises:
        ch.dispatch.side_effect = RuntimeError(f"{key} channel exploded")
    return ch


# ---------------------------------------------------------------------------
# _is_channel_enabled
# ---------------------------------------------------------------------------


class TestIsChannelEnabled:
    def test_missing_key_returns_true_by_default(self):
        """_is_channel_enabled returns True when the key is absent (default enabled)."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        assert svc._is_channel_enabled({}, "email") is True

    def test_explicit_false_returns_false(self):
        """_is_channel_enabled returns False when the key is explicitly False."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        assert svc._is_channel_enabled({"email": False}, "email") is False

    def test_explicit_true_returns_true(self):
        """_is_channel_enabled returns True when the key is explicitly True."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        assert svc._is_channel_enabled({"email": True}, "email") is True

    def test_in_app_key_missing_returns_true(self):
        """in_app defaults to True when not present in prefs dict."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        assert svc._is_channel_enabled({}, "in_app") is True

    def test_in_app_disabled_returns_false(self):
        """in_app returns False when explicitly set to False."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        assert svc._is_channel_enabled({"in_app": False}, "in_app") is False


# ---------------------------------------------------------------------------
# _get_prefs (new dict-returning version)
# ---------------------------------------------------------------------------


class TestGetPrefsV2:
    async def test_returns_channels_dict_from_preference(self):
        """_get_prefs returns the channels JSONB dict from the preference record."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        session = AsyncMock()

        pref = MagicMock()
        pref.channels = {"email": False, "in_app": True}

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = pref
        session.execute = AsyncMock(return_value=mock_result)

        prefs = await svc._get_prefs(session, user_id=1, project_id=1, event_type="assignment")
        assert prefs == {"email": False, "in_app": True}

    async def test_returns_empty_dict_when_no_preference_record(self):
        """_get_prefs returns {} when no preference row exists (all defaults apply)."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        session = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        prefs = await svc._get_prefs(session, user_id=99, project_id=1, event_type="assignment")
        assert prefs == {}

    async def test_default_preference_all_channels_enabled(self):
        """With empty prefs dict, _is_channel_enabled returns True for email and in_app."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        session = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        prefs = await svc._get_prefs(session, user_id=99, project_id=1, event_type="assignment")
        assert svc._is_channel_enabled(prefs, "email") is True
        assert svc._is_channel_enabled(prefs, "in_app") is True


# ---------------------------------------------------------------------------
# _build_assignment_payload
# ---------------------------------------------------------------------------


class TestBuildAssignmentPayload:
    def test_returns_notification_payload(self):
        """_build_assignment_payload returns a NotificationPayload instance."""
        from specivo.services.channels.base import NotificationPayload
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        issue = _make_issue(project_key="ACME", display_key="ACME-5", subject="Fix login")
        actor = _make_user(user_id=3, email="actor@example.com")
        actor.display_name = "Alice"

        payload = svc._build_assignment_payload(issue=issue, actor=actor)
        assert isinstance(payload, NotificationPayload)

    def test_event_type_is_assignment(self):
        """_build_assignment_payload sets event_type to 'assignment'."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        issue = _make_issue()
        actor = _make_user()
        payload = svc._build_assignment_payload(issue=issue, actor=actor)
        assert payload.event_type == "assignment"

    def test_entity_type_is_issue(self):
        """_build_assignment_payload sets entity_type to 'issue'."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        payload = svc._build_assignment_payload(issue=_make_issue(), actor=_make_user())
        assert payload.entity_type == "issue"

    def test_entity_id_matches_issue_id(self):
        """_build_assignment_payload sets entity_id from issue.id."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        issue = _make_issue(issue_id=42)
        payload = svc._build_assignment_payload(issue=issue, actor=_make_user())
        assert payload.entity_id == 42

    def test_project_id_matches_issue_project(self):
        """_build_assignment_payload sets project_id from issue.project_id."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        issue = _make_issue(project_id=7)
        payload = svc._build_assignment_payload(issue=issue, actor=_make_user())
        assert payload.project_id == 7

    def test_actor_id_matches_actor(self):
        """_build_assignment_payload sets actor_id from actor.id."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        actor = _make_user(user_id=99)
        payload = svc._build_assignment_payload(issue=_make_issue(), actor=actor)
        assert payload.actor_id == 99

    def test_title_contains_issue_key(self):
        """_build_assignment_payload title includes the issue display_key."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        issue = _make_issue(display_key="PROJ-7")
        payload = svc._build_assignment_payload(issue=issue, actor=_make_user())
        assert "PROJ-7" in payload.title

    def test_body_plain_is_set(self):
        """_build_assignment_payload populates body_plain."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        payload = svc._build_assignment_payload(issue=_make_issue(), actor=_make_user())
        assert payload.body_plain is not None
        assert len(payload.body_plain) > 0


# ---------------------------------------------------------------------------
# _build_comment_payload
# ---------------------------------------------------------------------------


class TestBuildCommentPayload:
    def test_returns_notification_payload(self):
        """_build_comment_payload returns a NotificationPayload instance."""
        from specivo.services.channels.base import NotificationPayload
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        issue = _make_issue()
        actor = _make_user()
        journal = _make_journal(notes="LGTM!")
        payload = svc._build_comment_payload(issue=issue, journal=journal, actor=actor)
        assert isinstance(payload, NotificationPayload)

    def test_event_type_is_comment(self):
        """_build_comment_payload sets event_type to 'comment'."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        payload = svc._build_comment_payload(issue=_make_issue(), journal=_make_journal(), actor=_make_user())
        assert payload.event_type == "comment"

    def test_comment_text_is_journal_notes(self):
        """_build_comment_payload sets comment_text from journal.notes."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        journal = _make_journal(notes="Please review the attached diff")
        payload = svc._build_comment_payload(issue=_make_issue(), journal=journal, actor=_make_user())
        assert payload.comment_text == "Please review the attached diff"

    def test_entity_type_is_issue(self):
        """_build_comment_payload sets entity_type to 'issue'."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        payload = svc._build_comment_payload(issue=_make_issue(), journal=_make_journal(), actor=_make_user())
        assert payload.entity_type == "issue"


# ---------------------------------------------------------------------------
# _dispatch_to_channels — in_app branch
# ---------------------------------------------------------------------------


class TestDispatchToChannelsInApp:
    async def test_creates_notification_when_in_app_enabled(self):
        """_dispatch_to_channels calls create_notification when in_app is enabled."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        session = AsyncMock()
        user = _make_user(user_id=2)
        issue = _make_issue()
        actor = _make_user(user_id=1)

        payload = svc._build_assignment_payload(issue=issue, actor=actor)

        with (
            patch.object(svc, "_get_prefs", return_value={}),
            patch.object(svc, "create_notification", new_callable=AsyncMock) as mock_create,
            patch(
                "specivo.services.notification_service.get_all_channels",
                return_value={},
            ),
        ):
            await svc._dispatch_to_channels(
                session,
                user=user,
                project_id=issue.project_id,
                event_type="assignment",
                payload=payload,
            )
            mock_create.assert_called_once()

    async def test_skips_in_app_when_disabled(self):
        """_dispatch_to_channels does NOT call create_notification when in_app=False."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        session = AsyncMock()
        user = _make_user(user_id=2)
        issue = _make_issue()
        actor = _make_user(user_id=1)

        payload = svc._build_assignment_payload(issue=issue, actor=actor)

        with (
            patch.object(svc, "_get_prefs", return_value={"in_app": False}),
            patch.object(svc, "create_notification", new_callable=AsyncMock) as mock_create,
            patch(
                "specivo.services.notification_service.get_all_channels",
                return_value={},
            ),
        ):
            await svc._dispatch_to_channels(
                session,
                user=user,
                project_id=issue.project_id,
                event_type="assignment",
                payload=payload,
            )
            mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# _dispatch_to_channels — external channel branch
# ---------------------------------------------------------------------------


class TestDispatchToChannelsEmail:
    async def test_calls_email_channel_dispatch_when_enabled(self):
        """_dispatch_to_channels calls EmailChannel.dispatch when email pref is enabled."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        session = AsyncMock()
        user = _make_user(user_id=2, email="user@example.com")
        issue = _make_issue()
        actor = _make_user(user_id=1)

        payload = svc._build_assignment_payload(issue=issue, actor=actor)
        email_ch = _make_channel("email", configured=True)

        with (
            patch.object(svc, "_get_prefs", return_value={"email": True, "in_app": False}),
            patch.object(svc, "create_notification", new_callable=AsyncMock),
            patch(
                "specivo.services.notification_service.get_all_channels",
                return_value={"email": email_ch},
            ),
        ):
            await svc._dispatch_to_channels(
                session,
                user=user,
                project_id=issue.project_id,
                event_type="assignment",
                payload=payload,
            )
            email_ch.dispatch.assert_called_once()

    async def test_skips_email_dispatch_when_pref_disabled(self):
        """_dispatch_to_channels skips EmailChannel when email=False in prefs."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        session = AsyncMock()
        user = _make_user(user_id=2)
        issue = _make_issue()
        actor = _make_user(user_id=1)

        payload = svc._build_assignment_payload(issue=issue, actor=actor)
        email_ch = _make_channel("email", configured=True)

        with (
            patch.object(svc, "_get_prefs", return_value={"email": False, "in_app": False}),
            patch.object(svc, "create_notification", new_callable=AsyncMock),
            patch(
                "specivo.services.notification_service.get_all_channels",
                return_value={"email": email_ch},
            ),
        ):
            await svc._dispatch_to_channels(
                session,
                user=user,
                project_id=issue.project_id,
                event_type="assignment",
                payload=payload,
            )
            email_ch.dispatch.assert_not_called()

    async def test_skips_channel_when_not_configured_for_user(self):
        """_dispatch_to_channels skips a channel when is_configured_for_user returns False."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        session = AsyncMock()
        user = _make_user(user_id=2)
        issue = _make_issue()
        actor = _make_user(user_id=1)

        payload = svc._build_assignment_payload(issue=issue, actor=actor)
        telegram_ch = _make_channel("telegram", configured=False)

        with (
            patch.object(svc, "_get_prefs", return_value={"in_app": False}),
            patch.object(svc, "create_notification", new_callable=AsyncMock),
            patch(
                "specivo.services.notification_service.get_all_channels",
                return_value={"telegram": telegram_ch},
            ),
        ):
            await svc._dispatch_to_channels(
                session,
                user=user,
                project_id=issue.project_id,
                event_type="assignment",
                payload=payload,
            )
            telegram_ch.dispatch.assert_not_called()

    async def test_per_channel_error_does_not_block_other_channels(self):
        """A dispatch exception for one channel must not prevent others from running."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        session = AsyncMock()
        user = _make_user(user_id=2)
        issue = _make_issue()
        actor = _make_user(user_id=1)

        payload = svc._build_assignment_payload(issue=issue, actor=actor)

        broken_ch = _make_channel("broken", configured=True, dispatch_raises=True)
        working_ch = _make_channel("working", configured=True)

        with (
            patch.object(svc, "_get_prefs", return_value={"in_app": False}),
            patch.object(svc, "create_notification", new_callable=AsyncMock),
            patch(
                "specivo.services.notification_service.get_all_channels",
                return_value={"broken": broken_ch, "working": working_ch},
            ),
        ):
            # Must NOT raise despite broken_ch failing
            await svc._dispatch_to_channels(
                session,
                user=user,
                project_id=issue.project_id,
                event_type="assignment",
                payload=payload,
            )
            working_ch.dispatch.assert_called_once()

    async def test_email_config_built_from_user_email(self):
        """For email channel, the user_config passed to dispatch includes user.email."""
        from specivo.services.notification_service import NotificationService

        svc = NotificationService()
        session = AsyncMock()
        user = _make_user(user_id=2, email="recipient@example.com")
        issue = _make_issue()
        actor = _make_user(user_id=1)

        payload = svc._build_assignment_payload(issue=issue, actor=actor)
        email_ch = _make_channel("email", configured=True)

        with (
            patch.object(svc, "_get_prefs", return_value={"in_app": False}),
            patch.object(svc, "create_notification", new_callable=AsyncMock),
            patch(
                "specivo.services.notification_service.get_all_channels",
                return_value={"email": email_ch},
            ),
        ):
            await svc._dispatch_to_channels(
                session,
                user=user,
                project_id=issue.project_id,
                event_type="assignment",
                payload=payload,
            )
            _, user_config_arg = email_ch.dispatch.call_args.args
            assert user_config_arg.get("email") == "recipient@example.com"
