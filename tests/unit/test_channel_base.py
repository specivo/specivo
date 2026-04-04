"""Unit tests for NotificationPayload dataclass and NotificationChannel Protocol.

RED phase — these tests define the expected behaviour of the channel base
module before any implementation exists.

Covers:
- NotificationPayload construction (required and optional fields)
- NotificationPayload is frozen (immutable)
- Structural subtyping: a class with the right methods IS a NotificationChannel
- Structural subtyping: a class missing methods is NOT a NotificationChannel
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# NotificationPayload
# ---------------------------------------------------------------------------


class TestNotificationPayload:
    def test_required_fields_accepted(self):
        """NotificationPayload can be constructed with all required fields."""
        from specivo.services.channels.base import NotificationPayload

        payload = NotificationPayload(
            event_type="assignment",
            entity_type="issue",
            entity_id=10,
            project_id=1,
            project_key="ACME",
            actor_name="Alice",
            actor_id=5,
            title="[ACME-10] Assigned to you by Alice",
            body_plain="Fix the login bug",
            body_html="<p>Fix the login bug</p>",
        )
        assert payload.event_type == "assignment"
        assert payload.entity_type == "issue"
        assert payload.entity_id == 10
        assert payload.project_id == 1
        assert payload.project_key == "ACME"
        assert payload.actor_name == "Alice"
        assert payload.actor_id == 5
        assert payload.title == "[ACME-10] Assigned to you by Alice"
        assert payload.body_plain == "Fix the login bug"
        assert payload.body_html == "<p>Fix the login bug</p>"

    def test_optional_fields_default_to_none(self):
        """Optional fields (issue_key, comment_text, etc.) default to None."""
        from specivo.services.channels.base import NotificationPayload

        payload = NotificationPayload(
            event_type="assignment",
            entity_type="issue",
            entity_id=1,
            project_id=1,
            project_key="ACME",
            actor_name="Alice",
            actor_id=5,
            title="Title",
            body_plain="Body",
            body_html=None,
        )
        assert payload.issue_key is None
        assert payload.issue_subject is None
        assert payload.comment_text is None
        assert payload.entity_url is None

    def test_optional_fields_accepted(self):
        """Optional fields can be set explicitly."""
        from specivo.services.channels.base import NotificationPayload

        payload = NotificationPayload(
            event_type="comment",
            entity_type="issue",
            entity_id=42,
            project_id=3,
            project_key="PROJ",
            actor_name="Bob",
            actor_id=7,
            title="[PROJ-42] New comment by Bob",
            body_plain="Please review",
            body_html="<p>Please review</p>",
            issue_key="PROJ-42",
            issue_subject="Fix the parser",
            comment_text="Please review this PR",
            entity_url="http://localhost/issues/PROJ-42",
        )
        assert payload.issue_key == "PROJ-42"
        assert payload.issue_subject == "Fix the parser"
        assert payload.comment_text == "Please review this PR"
        assert payload.entity_url == "http://localhost/issues/PROJ-42"

    def test_payload_is_frozen(self):
        """NotificationPayload must be immutable (frozen dataclass)."""
        from specivo.services.channels.base import NotificationPayload

        payload = NotificationPayload(
            event_type="assignment",
            entity_type="issue",
            entity_id=1,
            project_id=1,
            project_key="ACME",
            actor_name="Alice",
            actor_id=5,
            title="Title",
            body_plain="Body",
            body_html=None,
        )
        with pytest.raises((AttributeError, TypeError)):
            payload.event_type = "comment"  # type: ignore[misc]

    def test_body_html_can_be_none(self):
        """body_html is optional and can be None (plain-text-only channels)."""
        from specivo.services.channels.base import NotificationPayload

        payload = NotificationPayload(
            event_type="assignment",
            entity_type="issue",
            entity_id=1,
            project_id=1,
            project_key="ACME",
            actor_name="Alice",
            actor_id=5,
            title="Title",
            body_plain="Plain text body",
            body_html=None,
        )
        assert payload.body_html is None


# ---------------------------------------------------------------------------
# NotificationChannel Protocol
# ---------------------------------------------------------------------------


class TestNotificationChannelProtocol:
    def test_conforming_class_is_recognized(self):
        """A class that implements all three required members passes isinstance check."""
        from specivo.services.channels.base import NotificationChannel, NotificationPayload

        class MockChannel:
            @property
            def channel_key(self) -> str:
                return "mock"

            def is_configured_for_user(self, user_channel_config: dict | None) -> bool:
                return True

            def dispatch(self, payload: NotificationPayload, user_channel_config: dict) -> None:
                pass

        channel = MockChannel()
        assert isinstance(channel, NotificationChannel)

    def test_class_missing_dispatch_is_not_recognized(self):
        """A class that omits dispatch() is NOT a valid NotificationChannel."""
        from specivo.services.channels.base import NotificationChannel

        class IncompleteChannel:
            @property
            def channel_key(self) -> str:
                return "incomplete"

            def is_configured_for_user(self, user_channel_config: dict | None) -> bool:
                return False

            # dispatch() intentionally absent

        channel = IncompleteChannel()
        assert not isinstance(channel, NotificationChannel)

    def test_class_missing_channel_key_is_not_recognized(self):
        """A class that omits channel_key is NOT a valid NotificationChannel."""
        from specivo.services.channels.base import NotificationChannel, NotificationPayload

        class NoKeyChannel:
            # channel_key property intentionally absent

            def is_configured_for_user(self, user_channel_config: dict | None) -> bool:
                return False

            def dispatch(self, payload: NotificationPayload, user_channel_config: dict) -> None:
                pass

        channel = NoKeyChannel()
        assert not isinstance(channel, NotificationChannel)

    def test_class_missing_is_configured_is_not_recognized(self):
        """A class that omits is_configured_for_user() is NOT a valid NotificationChannel."""
        from specivo.services.channels.base import NotificationChannel, NotificationPayload

        class NoConfigCheckChannel:
            @property
            def channel_key(self) -> str:
                return "noop"

            # is_configured_for_user intentionally absent

            def dispatch(self, payload: NotificationPayload, user_channel_config: dict) -> None:
                pass

        channel = NoConfigCheckChannel()
        assert not isinstance(channel, NotificationChannel)

    def test_plain_object_is_not_recognized(self):
        """An arbitrary object with no matching interface is not a NotificationChannel."""
        from specivo.services.channels.base import NotificationChannel

        assert not isinstance(object(), NotificationChannel)
        assert not isinstance("string", NotificationChannel)
        assert not isinstance(42, NotificationChannel)
