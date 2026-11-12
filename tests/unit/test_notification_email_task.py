"""Unit tests for the ``send_notification_email`` task with SMTP mocked.

Covers:
- issue mail: every threading and X-Specivo-* header, Date, text and HTML parts
- non-issue mail (password reset): Message-ID, Date, Auto-Submitted, no threading
- a subject with CR/LF is sent sanitised and adds no header
"""

from __future__ import annotations

import smtplib
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
    smtp.__enter__.return_value = smtp
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


class _FakeSMTP:
    """SMTP stand-in that records whether the connection was closed.

    *fail* maps a method name (``login``, ``sendmail``, ``exit``) to the
    exception that call raises; ``exit`` is the QUIT sent when the ``with``
    block ends.
    """

    def __init__(self, fail: dict[str, BaseException] | None = None) -> None:
        self.fail = fail or {}
        self.closed = False
        self.sent = False

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise self.fail[name]

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.closed = True
        self._maybe_fail("exit")

    def close(self) -> None:
        self.closed = True

    def quit(self) -> None:
        self.closed = True
        self._maybe_fail("exit")

    def ehlo(self) -> None:
        pass

    def starttls(self, **_kwargs: object) -> None:
        pass

    def login(self, *_args: object) -> None:
        self._maybe_fail("login")

    def sendmail(self, *_args: object) -> dict:
        self._maybe_fail("sendmail")
        self.sent = True
        return {}


class _RetriedError(Exception):
    pass


def _run(smtp: object, *, smtp_user: str = "") -> MagicMock:
    """Run the task against *smtp* (an instance, or an exception the connect raises); return the retry mock."""
    from specivo.tasks.notifications import send_notification_email

    settings = Settings(  # type: ignore[call-arg]
        secret_key="k" * 64, smtp_from="noreply@mail.example.com", smtp_tls=False, smtp_user=smtp_user
    )
    connect = MagicMock(side_effect=smtp) if isinstance(smtp, BaseException) else MagicMock(return_value=smtp)
    retry = MagicMock(side_effect=_RetriedError())
    with (
        patch("specivo.tasks.notifications.get_settings", return_value=settings),
        patch("specivo.core.mail_headers.get_settings", return_value=settings),
        patch("specivo.tasks.notifications.smtplib.SMTP", connect),
        patch.object(send_notification_email, "retry", retry),
    ):
        send_notification_email("bob@example.com", "Hello", "<p>Hi</p>")
    return retry


class TestSmtpErrors:
    def test_connection_is_closed_when_sending_fails(self):
        smtp = _FakeSMTP({"sendmail": smtplib.SMTPServerDisconnected("gone")})
        with pytest.raises(_RetriedError):
            _run(smtp)
        assert smtp.closed

    @pytest.mark.parametrize(
        "error",
        [
            ConnectionRefusedError("refused"),
            TimeoutError("timed out"),
            smtplib.SMTPConnectError(421, b"busy"),
        ],
    )
    def test_connection_errors_are_retried(self, error):
        with pytest.raises(_RetriedError):
            _run(error)

    @pytest.mark.parametrize(
        ("method", "error"),
        [
            ("sendmail", smtplib.SMTPServerDisconnected("gone")),
            ("sendmail", smtplib.SMTPDataError(451, b"try again later")),
            ("sendmail", smtplib.SMTPRecipientsRefused({"bob@example.com": (450, b"mailbox busy")})),
            ("sendmail", smtplib.SMTPSenderRefused(421, b"busy", "noreply@mail.example.com")),
        ],
    )
    def test_transient_errors_are_retried(self, method, error):
        with pytest.raises(_RetriedError):
            _run(_FakeSMTP({method: error}), smtp_user="mailer")

    @pytest.mark.parametrize(
        ("method", "error"),
        [
            ("login", smtplib.SMTPAuthenticationError(535, b"bad credentials")),
            ("sendmail", smtplib.SMTPRecipientsRefused({"bob@example.com": (550, b"no such user")})),
            ("sendmail", smtplib.SMTPDataError(554, b"rejected")),
            ("sendmail", smtplib.SMTPSenderRefused(553, b"not allowed", "noreply@mail.example.com")),
            ("login", smtplib.SMTPNotSupportedError("AUTH not supported")),
        ],
    )
    def test_permanent_errors_are_not_retried(self, method, error):
        # The error itself propagates (the task fails), not the retry.
        smtp = _FakeSMTP({method: error})
        with pytest.raises(type(error)):
            _run(smtp, smtp_user="mailer")
        assert smtp.closed

    def test_quit_failure_after_send_is_not_retried(self):
        smtp = _FakeSMTP({"exit": smtplib.SMTPResponseException(421, b"closing")})
        retry = _run(smtp)
        retry.assert_not_called()
        assert smtp.sent
