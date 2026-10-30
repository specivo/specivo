"""Hardening of the outgoing mail headers (``specivo.core.mail_headers``).

Covers:
- every Unicode line break and control character is stripped from header values,
  so a crafted issue subject can neither add a header nor make the send fail
- SMTP_MESSAGE_ID_DOMAIN must be a plain host name; an unusable from-address
  domain falls back to a safe one
- every mail is multipart/alternative with a text and an HTML part
- the issue tag is not doubled whatever its case
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from specivo.core.config import Settings
from specivo.core.mail_headers import build_message, issue_email_subject, mail_domain, new_message_id

pytestmark = pytest.mark.unit

_MSGID_RE = re.compile(r"^<[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[a-z0-9.-]+>$")
_LINE_BREAKS = ["\r", "\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "]


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"secret_key": "k" * 64, "smtp_from": "noreply@mail.example.com"}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _patched_settings():
    with patch("specivo.core.mail_headers.get_settings", return_value=_settings()) as mock:
        yield mock


class TestLineBreaksInHeaders:
    @pytest.mark.parametrize("brk", _LINE_BREAKS)
    def test_subject_with_any_line_break_builds_and_stays_one_header(self, brk):
        subject = issue_email_subject("ACME-1", f"Fix{brk}Bcc: x@example.com")
        msg = build_message(
            from_addr="noreply@mail.example.com",
            to_addr="to@example.com",
            subject=subject,
            body_html="<p>x</p>",
            body_plain="x",
        )
        raw = msg.as_string()
        assert "Bcc" not in msg
        assert not re.search(r"^Bcc:", raw, re.MULTILINE)
        assert brk not in str(msg["Subject"])

    def test_control_characters_are_stripped(self):
        subject = issue_email_subject("ACME-1", "Fix\x00\x07\x1b[31m login\x7f")
        assert not re.search(r"[\x00-\x1f\x7f]", subject)


class TestMessageIdDomainSetting:
    @pytest.mark.parametrize(
        "value",
        [
            "example.com>\r\nBcc: x@example.com",
            "exa mple.com",
            "noreply@example.com",
            "<example.com>",
            "example..com",
            "-example.com",
            "example.com.",
            "exämple.com",
        ],
    )
    def test_invalid_domain_is_rejected(self, value):
        with pytest.raises(ValidationError):
            _settings(smtp_message_id_domain=value)

    def test_valid_domain_is_normalised(self):
        assert _settings(smtp_message_id_domain=" Mail.Example.COM ").smtp_message_id_domain == "mail.example.com"

    def test_empty_means_default(self):
        assert _settings(smtp_message_id_domain="").smtp_message_id_domain == ""

    def test_unusable_from_domain_falls_back_to_app_url(self, _patched_settings):
        _patched_settings.return_value = _settings(
            smtp_from="Specivo <noreply@[192.0.2.1]>", app_url="https://tracker.example.org/"
        )
        assert mail_domain() == "tracker.example.org"
        assert _MSGID_RE.match(new_message_id(1, "comment"))

    def test_last_resort_is_localhost(self, _patched_settings):
        _patched_settings.return_value = _settings(smtp_from="nobody", app_url="not a url")
        assert mail_domain() == "localhost"


class TestAlwaysAlternative:
    def _parts(self, **kwargs: str) -> list[str]:
        msg = build_message(from_addr="noreply@mail.example.com", to_addr="to@example.com", subject="s", **kwargs)
        assert msg.get_content_type() == "multipart/alternative"
        return [p.get_content_type() for p in msg.iter_parts()]

    def test_text_only_gets_an_html_part(self):
        assert self._parts(body_plain="a <b> & c") == ["text/plain", "text/html"]

    def test_text_only_html_part_is_escaped(self):
        msg = build_message(
            from_addr="noreply@mail.example.com", to_addr="to@example.com", subject="s", body_plain="a <b> & c"
        )
        html = msg.get_body(("html",)).get_content()  # type: ignore[union-attr]
        assert "a &lt;b&gt; &amp; c" in html

    def test_html_only_gets_a_text_part(self):
        msg = build_message(
            from_addr="noreply@mail.example.com",
            to_addr="to@example.com",
            subject="s",
            body_html="<p>Hello &amp; welcome</p><p>Second</p>",
        )
        assert [p.get_content_type() for p in msg.iter_parts()] == ["text/plain", "text/html"]
        text = msg.get_body(("plain",)).get_content()  # type: ignore[union-attr]
        assert "Hello & welcome" in text and "Second" in text and "<p>" not in text


class TestSubjectTag:
    def test_does_not_double_tag_in_other_case(self):
        assert issue_email_subject("ACME-1", "[acme-1] Fix login") == "[ACME-1] Fix login"
