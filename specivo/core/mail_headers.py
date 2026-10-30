"""Headers for outgoing mail: identifiers, per-issue threading, auto-reply suppression.

Modelled on Request Tracker's outgoing mail. Every mail about one issue answers
the same thread root, ``<specivo-issue-{id}@{domain}>``, which is never sent as
a Message-ID itself; the root and the per-mail Message-ID both carry the
internal issue id, so a reply can later be mapped back to its issue without a
lookup table, across project renames and issue moves.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from email.message import EmailMessage
from email.policy import EmailPolicy
from email.utils import formatdate, parseaddr
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

from specivo.core.config import get_settings, is_mail_domain

# Header names a caller may set on top of From/To/Subject/Date.
ALLOWED_HEADERS = frozenset(
    {
        "Message-ID",
        "In-Reply-To",
        "References",
        "Auto-Submitted",
        "X-Auto-Response-Suppress",
        "X-Specivo-Loop",
        "X-Specivo-Issue",
        "X-Specivo-Project",
        "X-Specivo-Event",
        "List-Id",
    }
)

# Notification event types (preference keys) to the X-Specivo-Event values.
_MAIL_EVENTS = {
    "assignment": "assignment",
    "status_change": "updated",
    "updated": "updated",
    "comment": "comment",
    "mention": "mention",
}

# Identifiers stay on one line: some clients do not unfold them when threading.
# RFC 5322 allows 998 characters, far more than any of them takes.
_UNFOLDED_HEADERS = frozenset({"message-id", "in-reply-to", "references", "list-id"})

# CR, LF and every other character str.splitlines() breaks on (the email
# package refuses a header value containing any of them), plus the remaining
# control characters.
_LINE_BREAKS_AND_CONTROLS = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]+")
_UNSAFE_ID_CHARS = re.compile(r"[^a-z0-9-]")
_UNSAFE_LIST_CHARS = re.compile(r"[^a-z0-9_-]")


class _MailPolicy(EmailPolicy):
    def fold(self, name: str, value: str) -> str:
        if name.lower() in _UNFOLDED_HEADERS:
            return f"{name}: {value}{self.linesep}"
        folded: str = super().fold(name, value)
        return folded


_POLICY = _MailPolicy()


def strip_crlf(value: str) -> str:
    """Replace every run of line breaks or control characters with one space.

    The value then fits in one header, and the email package accepts it.
    """
    return _LINE_BREAKS_AND_CONTROLS.sub(" ", value).strip()


def mail_domain() -> str:
    """Domain for Message-IDs and List-Id.

    ``SMTP_MESSAGE_ID_DOMAIN`` (validated when the settings load), else the
    from address's domain, else the ``APP_URL`` host, as long as each is a
    plain host name; ``localhost`` as the last resort.
    """
    settings = get_settings()
    if settings.smtp_message_id_domain:
        return settings.smtp_message_id_domain
    _, address = parseaddr(settings.smtp_from)
    _, at, from_domain = address.rpartition("@")
    candidates = [from_domain if at else "", urlsplit(settings.app_url).hostname or ""]
    for candidate in candidates:
        domain = candidate.strip().lower()
        if is_mail_domain(domain):
            return domain
    return "localhost"


def instance_id() -> str:
    """A stable, non-secret id of this installation, for ``X-Specivo-Loop``.

    Derived one-way from ``SECRET_KEY``, so it stays the same across restarts
    and differs between installations without revealing the key.
    """
    key = get_settings().secret_key.encode()
    return hmac.new(key, b"specivo-mail-loop", hashlib.sha256).hexdigest()[:16]


def mail_event(event_type: str) -> str:
    """The ``X-Specivo-Event`` value for a notification event type."""
    return _MAIL_EVENTS.get(event_type, "updated")


def issue_thread_root(issue_id: int) -> str:
    """The thread root every mail about *issue_id* refers to."""
    return f"<specivo-issue-{int(issue_id)}@{mail_domain()}>"


def new_message_id(issue_id: int | None = None, event: str | None = None) -> str:
    """A new, unique Message-ID; for issue mail it carries the issue id and event."""
    parts = ["specivo"]
    if issue_id is not None:
        parts.append(str(int(issue_id)))
        if event:
            parts.append(_UNSAFE_ID_CHARS.sub("-", event.lower()))
    parts.append(uuid.uuid4().hex)
    return f"<{'.'.join(parts)}@{mail_domain()}>"


def issue_email_subject(issue_key: str, subject: str) -> str:
    """``[KEY-123] subject``, the same for every mail about one issue."""
    key = strip_crlf(issue_key)
    text = strip_crlf(subject)
    tag = f"[{key}]"
    if text.casefold().startswith(tag.casefold()):
        text = text[len(tag) :].lstrip()
    return f"{tag} {text}".rstrip()


def notification_headers(*, issue_id: int, issue_key: str, project_key: str, event_type: str) -> dict[str, str]:
    """Headers for one notification mail about an issue (``Date`` is added when sending)."""
    event = mail_event(event_type)
    root = issue_thread_root(issue_id)
    list_name = _UNSAFE_LIST_CHARS.sub("-", project_key.lower())
    return {
        "Message-ID": new_message_id(issue_id, event),
        "In-Reply-To": root,
        "References": root,
        "Auto-Submitted": "auto-generated",
        "X-Auto-Response-Suppress": "All",
        "X-Specivo-Loop": instance_id(),
        "X-Specivo-Issue": strip_crlf(issue_key),
        "X-Specivo-Project": strip_crlf(project_key),
        "X-Specivo-Event": event,
        "List-Id": f"<{list_name}.{mail_domain()}>",
    }


def build_message(
    *,
    from_addr: str,
    to_addr: str,
    subject: str,
    body_html: str | None = None,
    body_plain: str | None = None,
    headers: dict[str, str] | None = None,
) -> EmailMessage:
    """Build a ``multipart/alternative`` mail with a text and an HTML part.

    A missing part is made from the other one: the text escaped into HTML, or
    the HTML reduced to its text.

    Only names in ``ALLOWED_HEADERS`` are taken from *headers*; any mail gets a
    ``Message-ID``, ``Date`` and ``Auto-Submitted`` if they are missing. Every
    value has CR/LF stripped, so no input can add a header.
    """
    msg = EmailMessage(policy=_POLICY)
    msg["Subject"] = strip_crlf(subject)
    msg["From"] = strip_crlf(from_addr)
    msg["To"] = strip_crlf(to_addr)
    msg["Date"] = formatdate(localtime=False, usegmt=True)
    extra = {name: value for name, value in (headers or {}).items() if name in ALLOWED_HEADERS}
    extra.setdefault("Message-ID", new_message_id())
    extra.setdefault("Auto-Submitted", "auto-generated")
    for name, value in extra.items():
        msg[name] = strip_crlf(value)

    if body_plain is None:
        body_plain = _html_to_text(body_html or "")
    if body_html is None:
        body_html = "".join(f"<p>{escape(p)}</p>" for p in body_plain.split("\n\n") if p.strip())
    msg.set_content(body_plain)
    msg.add_alternative(body_html, subtype="html")
    return msg


class _TextExtractor(HTMLParser):
    _BLOCKS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "blockquote", "pre"})
    _SKIP = frozenset({"head", "style", "script", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skipping = max(0, self._skipping - 1)
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def _html_to_text(body_html: str) -> str:
    """The text of an HTML body, one paragraph per block, for the text part."""
    parser = _TextExtractor()
    parser.feed(body_html)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text + "\n" if text else ""
