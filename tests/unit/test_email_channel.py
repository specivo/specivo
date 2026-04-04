"""Unit tests for EmailChannel.

RED phase — these tests define the expected behaviour of the email channel
adapter before any implementation exists.

Covers:
- channel_key is "email"
- is_configured_for_user: valid config dict → True
- is_configured_for_user: None → False
- is_configured_for_user: empty dict → False
- is_configured_for_user: dict without "email" key → False
- dispatch() calls send_notification_email.delay with correct args
- dispatch() passes body_html when available
- dispatch() falls back to <p>{body_plain}</p> when body_html is None
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_payload(*, body_html: str | None = "<p>Fix it</p>", body_plain: str = "Fix it"):
    """Build a minimal NotificationPayload for testing."""
    from specivo.services.channels.base import NotificationPayload

    return NotificationPayload(
        event_type="assignment",
        entity_type="issue",
        entity_id=10,
        project_id=1,
        project_key="ACME",
        actor_name="Alice",
        actor_id=5,
        title="[ACME-10] Assigned to you by Alice",
        body_plain=body_plain,
        body_html=body_html,
    )


# ---------------------------------------------------------------------------
# channel_key
# ---------------------------------------------------------------------------


class TestEmailChannelKey:
    def test_channel_key_is_email(self):
        """EmailChannel.channel_key must return the string 'email'."""
        from specivo.services.channels.email_channel import EmailChannel

        ch = EmailChannel()
        assert ch.channel_key == "email"


# ---------------------------------------------------------------------------
# is_configured_for_user
# ---------------------------------------------------------------------------


class TestEmailChannelIsConfigured:
    def test_valid_config_returns_true(self):
        """Dict with an 'email' key returns True."""
        from specivo.services.channels.email_channel import EmailChannel

        ch = EmailChannel()
        assert ch.is_configured_for_user({"email": "user@example.com"}) is True

    def test_none_returns_false(self):
        """None config returns False."""
        from specivo.services.channels.email_channel import EmailChannel

        ch = EmailChannel()
        assert ch.is_configured_for_user(None) is False

    def test_empty_dict_returns_false(self):
        """Empty dict config returns False."""
        from specivo.services.channels.email_channel import EmailChannel

        ch = EmailChannel()
        assert ch.is_configured_for_user({}) is False

    def test_dict_without_email_key_returns_false(self):
        """Config dict that lacks the 'email' key returns False."""
        from specivo.services.channels.email_channel import EmailChannel

        ch = EmailChannel()
        assert ch.is_configured_for_user({"phone": "+1234567890"}) is False

    def test_empty_string_email_returns_false(self):
        """Config dict with an empty-string email value returns False."""
        from specivo.services.channels.email_channel import EmailChannel

        ch = EmailChannel()
        assert ch.is_configured_for_user({"email": ""}) is False


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


class TestEmailChannelDispatch:
    def test_dispatch_calls_celery_task_delay(self):
        """dispatch() must call send_notification_email.delay exactly once."""
        from specivo.services.channels.email_channel import EmailChannel

        ch = EmailChannel()
        payload = _make_payload()

        with patch("specivo.tasks.notifications.send_notification_email.delay") as mock_delay:
            ch.dispatch(payload, {"email": "user@example.com"})
            mock_delay.assert_called_once()

    def test_dispatch_passes_correct_recipient_email(self):
        """The first argument to .delay() must be the recipient email."""
        from specivo.services.channels.email_channel import EmailChannel

        ch = EmailChannel()
        payload = _make_payload()

        with patch("specivo.tasks.notifications.send_notification_email.delay") as mock_delay:
            ch.dispatch(payload, {"email": "recipient@example.com"})
            to_addr = mock_delay.call_args.args[0]
            assert to_addr == "recipient@example.com"

    def test_dispatch_passes_payload_title_as_subject(self):
        """The second argument to .delay() must be payload.title."""
        from specivo.services.channels.email_channel import EmailChannel

        ch = EmailChannel()
        payload = _make_payload()

        with patch("specivo.tasks.notifications.send_notification_email.delay") as mock_delay:
            ch.dispatch(payload, {"email": "user@example.com"})
            subject = mock_delay.call_args.args[1]
            assert subject == payload.title

    def test_dispatch_uses_body_html_when_available(self):
        """The third argument must be body_html when it is not None."""
        from specivo.services.channels.email_channel import EmailChannel

        ch = EmailChannel()
        payload = _make_payload(body_html="<h1>Rich body</h1>", body_plain="Rich body")

        with patch("specivo.tasks.notifications.send_notification_email.delay") as mock_delay:
            ch.dispatch(payload, {"email": "user@example.com"})
            body_arg = mock_delay.call_args.args[2]
            assert body_arg == "<h1>Rich body</h1>"

    def test_dispatch_falls_back_to_body_plain_wrapped_in_p(self):
        """When body_html is None, body_plain is wrapped in <p>...</p>."""
        from specivo.services.channels.email_channel import EmailChannel

        ch = EmailChannel()
        payload = _make_payload(body_html=None, body_plain="Plain text body")

        with patch("specivo.tasks.notifications.send_notification_email.delay") as mock_delay:
            ch.dispatch(payload, {"email": "user@example.com"})
            body_arg = mock_delay.call_args.args[2]
            assert body_arg == "<p>Plain text body</p>"
