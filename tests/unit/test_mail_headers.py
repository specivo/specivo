"""Unit tests for the outgoing mail headers (``specivo.core.mail_headers``).

Covers:
- mail_domain defaults to the from-address domain; SMTP_MESSAGE_ID_DOMAIN wins
- new_message_id is unique, well formed and carries the issue id
- issue_thread_root is deterministic per issue
- issue_email_subject strips CR/LF and does not double the issue tag
- notification_headers: threading, auto-reply suppression and X-Specivo-* headers
- mail_event maps notification event types onto the header values
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from specivo.core.config import Settings
from specivo.core.mail_headers import (
    issue_email_subject,
    issue_thread_root,
    mail_domain,
    mail_event,
    new_message_id,
    notification_headers,
)

pytestmark = pytest.mark.unit

_MSGID_RE = re.compile(r"^<[^<>@\s]+@[^<>@\s]+>$")


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"secret_key": "k" * 64, "smtp_from": "noreply@mail.example.com"}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _patched_settings():
    with patch("specivo.core.mail_headers.get_settings", return_value=_settings()) as mock:
        yield mock


class TestMailDomain:
    def test_defaults_to_from_address_domain(self):
        assert mail_domain() == "mail.example.com"

    def test_from_address_with_display_name(self, _patched_settings):
        _patched_settings.return_value = _settings(smtp_from="Specivo <Notify@Tracker.Example.org>")
        assert mail_domain() == "tracker.example.org"

    def test_setting_wins(self, _patched_settings):
        _patched_settings.return_value = _settings(smtp_message_id_domain="ids.example.net")
        assert mail_domain() == "ids.example.net"


class TestMessageIds:
    def test_new_message_id_is_unique(self):
        ids = {new_message_id(42, "comment") for _ in range(200)}
        assert len(ids) == 200

    def test_new_message_id_format(self):
        mid = new_message_id(42, "comment")
        assert _MSGID_RE.match(mid), mid
        assert mid.endswith("@mail.example.com>")
        assert mid.startswith("<specivo.42.comment.")
        assert not any(c in mid for c in " \r\n\t")

    def test_new_message_id_without_issue(self):
        mid = new_message_id()
        assert _MSGID_RE.match(mid), mid
        assert mid.startswith("<specivo.")

    def test_new_message_id_sanitises_event(self):
        mid = new_message_id(7, "bad event\r\nX: y")
        assert _MSGID_RE.match(mid), mid
        assert not any(c in mid for c in " \r\n:")

    def test_thread_root_is_deterministic(self):
        assert issue_thread_root(42) == issue_thread_root(42) == "<specivo-issue-42@mail.example.com>"
        assert issue_thread_root(43) != issue_thread_root(42)


class TestSubject:
    def test_adds_tag(self):
        assert issue_email_subject("ACME-1", "Fix login") == "[ACME-1] Fix login"

    def test_does_not_double_tag(self):
        assert issue_email_subject("ACME-1", "[ACME-1] Fix login") == "[ACME-1] Fix login"

    def test_strips_cr_lf(self):
        subject = issue_email_subject("ACME-1", "Fix\r\nBcc: x@example.com")
        assert "\r" not in subject and "\n" not in subject
        assert subject == "[ACME-1] Fix Bcc: x@example.com"

    def test_strips_cr_lf_from_key(self):
        assert "\n" not in issue_email_subject("ACME-1\n", "Fix")


class TestMailEvent:
    @pytest.mark.parametrize(
        ("event_type", "expected"),
        [
            ("assignment", "assignment"),
            ("status_change", "updated"),
            ("updated", "updated"),
            ("comment", "comment"),
            ("mention", "mention"),
            ("something_else", "updated"),
        ],
    )
    def test_maps_event_types(self, event_type, expected):
        assert mail_event(event_type) == expected


class TestNotificationHeaders:
    def _headers(self) -> dict[str, str]:
        return notification_headers(issue_id=42, issue_key="ACME-7", project_key="ACME", event_type="comment")

    def test_threading_headers(self):
        h = self._headers()
        root = "<specivo-issue-42@mail.example.com>"
        assert h["In-Reply-To"] == root
        assert h["References"] == root
        assert h["Message-ID"] != root
        assert h["Message-ID"].startswith("<specivo.42.comment.")

    def test_auto_reply_suppression(self):
        h = self._headers()
        assert h["Auto-Submitted"] == "auto-generated"
        assert h["X-Auto-Response-Suppress"] == "All"
        assert "Precedence" not in h

    def test_specivo_headers(self):
        h = self._headers()
        assert h["X-Specivo-Issue"] == "ACME-7"
        assert h["X-Specivo-Project"] == "ACME"
        assert h["X-Specivo-Event"] == "comment"
        assert re.fullmatch(r"[0-9a-f]{16}", h["X-Specivo-Loop"])

    def test_loop_id_is_stable_and_not_the_secret(self):
        assert self._headers()["X-Specivo-Loop"] == self._headers()["X-Specivo-Loop"]
        assert "k" * 16 not in self._headers()["X-Specivo-Loop"]

    def test_list_id_is_lowercase_and_valid(self):
        h = notification_headers(issue_id=1, issue_key="MyProj-1", project_key="MyProj", event_type="assignment")
        assert h["List-Id"] == "<myproj.mail.example.com>"
        assert re.fullmatch(r"<[a-z0-9_.-]+>", h["List-Id"])

    def test_no_version_or_actor_address(self):
        values = " ".join(self._headers().values())
        assert "@" not in values.replace("@mail.example.com", "")
        assert not any(k.lower() in {"x-mailer", "user-agent"} for k in self._headers())
