"""The provider interface and the data it parses webhook payloads into.

A provider adapter turns one forge's webhook into Specivo's terms: it checks
the signature or token on the raw body, names the event, finds the delivery
id, and parses a push into a :class:`PushEvent`. Adapters are stateless and
never touch the database.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

#: ``repository_deliveries.delivery_id`` is VARCHAR(100); a longer id is replaced by the body hash.
DELIVERY_ID_MAX_LENGTH = 100

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class SignatureError(Exception):
    """The request's signature or token is missing or does not match the secret.

    The message is for logs and never contains the secret or the header value.
    """


@dataclass(frozen=True)
class CommitData:
    """One commit of a push, as the forge describes it. ``sha`` is lower-case hex (SHA-1 or SHA-256)."""

    sha: str
    message: str
    url: str
    author_name: str
    author_email: str
    timestamp: datetime | None


@dataclass(frozen=True)
class PushEvent:
    """A push to one ref. ``branch`` is None for anything but a branch (a tag push)."""

    repo_full_name: str | None
    ref: str
    branch: str | None
    default_branch: str | None
    commits: tuple[CommitData, ...]
    pusher_login: str | None


class ForgeProvider(Protocol):
    """What the incoming hook endpoint needs from a forge."""

    name: str

    def verify(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> None:
        """Raise :class:`SignatureError` unless the request is signed with *secret*."""
        ...

    def event_type(self, headers: Mapping[str, str]) -> str | None:
        """The event name, normalised for the ones Specivo handles (``push``, ``pull_request``, ``ping``)."""
        ...

    def delivery_id(self, headers: Mapping[str, str], raw_body: bytes) -> str:
        """The forge's delivery id, or a hash of the body when the forge sends none."""
        ...

    def repo_full_name(self, payload: dict[str, Any]) -> str | None:
        """``owner/repo`` of the repository the payload is about, if it names one."""
        ...

    def parse_push(self, payload: dict[str, Any]) -> PushEvent:
        """Parse a push payload; commits without a valid id are left out."""
        ...


def header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup that works for plain dicts too."""
    value = headers.get(name)
    if value is not None:
        return value
    wanted = name.lower()
    for key, val in headers.items():
        if key.lower() == wanted:
            return val
    return None


def constant_time_equal(given: str, expected: str) -> bool:
    """``hmac.compare_digest`` on bytes, so a non-ASCII header cannot raise."""
    return hmac.compare_digest(given.encode("utf-8", "surrogateescape"), expected.encode())


def hmac_sha256_hex(secret: str, raw_body: bytes) -> str:
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def body_delivery_id(raw_body: bytes) -> str:
    """The fallback delivery id: identical bodies are the same delivery."""
    return "sha256:" + hashlib.sha256(raw_body).hexdigest()


def delivery_id_from(headers: Mapping[str, str], names: tuple[str, ...], raw_body: bytes) -> str:
    """The first non-empty header of *names*, hashed when too long; else the body hash."""
    for name in names:
        value = (header(headers, name) or "").strip()
        if value:
            if len(value) > DELIVERY_ID_MAX_LENGTH:
                return "sha256:" + hashlib.sha256(value.encode()).hexdigest()
            return value
    return body_delivery_id(raw_body)


def branch_of(ref: str) -> str | None:
    return ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else None


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def dict_at(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def parse_commits(raw: Any) -> tuple[CommitData, ...]:
    """The ``commits`` array shared by GitHub, GitLab, Gitea and Forgejo push payloads."""
    if not isinstance(raw, list):
        return ()
    commits: list[CommitData] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        sha = _str(item.get("id")).lower()
        if not _SHA.fullmatch(sha):
            continue
        author = dict_at(item, "author")
        commits.append(
            CommitData(
                sha=sha,
                message=_str(item.get("message")),
                url=_str(item.get("url")),
                author_name=_str(author.get("name")) or _str(author.get("username")),
                author_email=_str(author.get("email")),
                timestamp=_timestamp(item.get("timestamp")),
            )
        )
    return tuple(commits)


def str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
