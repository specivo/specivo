"""Unit tests for the ``send_notification_email`` task with SMTP mocked.

Covers:
- issue mail: every threading and X-Specivo-* header, Date, text and HTML parts
- non-issue mail (password reset): Message-ID, Date, Auto-Submitted, no threading
- a subject with CR/LF is sent sanitised and adds no header
"""

from __future__ import annotations

from email import message_from_string, policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from unittest.mock import MagicMock, patch

import pytest

from specivo.core.config import Settings
from specivo.core.mail_headers import notification_headers

pytestmark = pytest.mark.unit


def _settings() -> Settings:
    return Settings(secret_key="k" * 64, smtp_from="noreply@mail.example.com", smtp_tls=False)  # type: ignore[call-arg]


def _send(*args: object, **kwargs: object) -> EmailMessage:
    """Run the task with SMTP mocked and return the parsed message it sent."""
    from specivo.tasks.notifications import send_notification_email

    smtp = MagicMock()
    with (
        patch("specivo.tasks.notifications.get_settings", return_value=_settings()),
        patch("specivo.core.mail_headers.get_settings", return_value=_settings()),
        patch("specivo.tasks.notifications.smtplib.SMTP", return_value=smtp),
    ):
        send_notification_email(*args, **kwargs)
    smtp.sendmail.assert_called_once()
    raw = smtp.sendmail.call_args.args[2]
    return message_from_string(raw, policy=policy.default)  # type: ignore[return-value]


def _issue_headers() -> dict[str, str]:
    with patch("specivo.core.mail_headers.get_settings", return_value=_settings()):
        return notification_headers(issue_id=42, issue_key="ACME-7", project_key="ACME", event_type="comment")


class TestIssueMail:
    def test_headers(self):
        headers = _issue_headers()
        msg = _send(
            "bob@example.com",
            "[ACME-7] Fix login",
            "<p>Alice commented</p>",
            body_plain="Alice commented\n\nLooks good",
            headers=headers,
        )
        assert msg["Subject"] == "[ACME-7] Fix login"
        assert msg["From"] == "noreply@mail.example.com"
        assert msg["To"] == "bob@example.com"
        assert msg["Message-ID"] == headers["Message-ID"]
        assert msg["In-Reply-To"] == "<specivo-issue-42@mail.example.com>"
        assert msg["References"] == "<specivo-issue-42@mail.example.com>"
        assert msg["Auto-Submitted"] == "auto-generated"
        assert msg["X-Auto-Response-Suppress"] == "All"
        assert msg["X-Specivo-Loop"] == headers["X-Specivo-Loop"]
        assert msg["X-Specivo-Issue"] == "ACME-7"
        assert msg["X-Specivo-Project"] == "ACME"
        assert msg["X-Specivo-Event"] == "comment"
        assert msg["List-Id"] == "<acme.mail.example.com>"
        assert msg["Precedence"] is None
        assert parsedate_to_datetime(msg["Date"]).tzinfo is not None

    def test_text_and_html_parts(self):
        msg = _send(
            "bob@example.com",
            "[ACME-7] Fix login",
            "<p>Alice commented</p>",
            body_plain="Alice commented\n\nLooks good",
            headers=_issue_headers(),
        )
        assert msg.get_content_type() == "multipart/alternative"
        plain = msg.get_body(preferencelist=("plain",))
        html = msg.get_body(preferencelist=("html",))
        assert plain is not None and html is not None
        assert plain.get_content().startswith("Alice commented\n\nLooks good")
        assert "<p>Alice commented</p>" in html.get_content()

    def test_subject_with_crlf_is_sanitised(self):
        msg = _send(
            "bob@example.com",
            "[ACME-7] Fix\r\nBcc: x@example.com",
            "<p>x</p>",
            body_plain="x",
            headers=_issue_headers(),
        )
        assert msg["Bcc"] is None
        assert "\n" not in str(msg["Subject"])
        assert str(msg["Subject"]) == "[ACME-7] Fix Bcc: x@example.com"


class TestNonIssueMail:
    def test_password_reset_headers(self):
        msg = _send("bob@example.com", "Password reset request", "<p>Reset</p>", body_plain="Reset")
        assert msg["Message-ID"].endswith("@mail.example.com>")
        assert msg["Auto-Submitted"] == "auto-generated"
        assert msg["Date"] is not None
        for name in ("References", "In-Reply-To", "X-Specivo-Issue", "List-Id", "X-Auto-Response-Suppress"):
            assert msg[name] is None, name

    def test_html_only_still_sends(self):
        msg = _send("bob@example.com", "Hello", "<p>Hi</p>")
        # The text part is made from the HTML: every mail is multipart/alternative.
        assert msg.get_content_type() == "multipart/alternative"
        assert msg.get_body(("plain",)).get_content().strip() == "Hi"  # type: ignore[union-attr]
        assert msg["Message-ID"] is not None

    def test_caller_headers_cannot_override_addressing(self):
        msg = _send(
            "bob@example.com",
            "Hello",
            "<p>Hi</p>",
            headers={"To": "eve@example.com", "Bcc": "eve@example.com", "X-Specivo-Event": "comment"},
        )
        assert msg["To"] == "bob@example.com"
        assert msg["Bcc"] is None
        assert msg["X-Specivo-Event"] == "comment"
