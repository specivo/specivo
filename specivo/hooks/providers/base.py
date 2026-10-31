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
#: ``pull_requests.title`` is VARCHAR(1000).
PR_TITLE_MAX_LENGTH = 1000
#: Pull request bodies are only read for references; the rest is never stored.
PR_BODY_MAX_LENGTH = 10_000

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


@dataclass(frozen=True)
class PullRequestEvent:
    """A pull/merge request as one event describes it.

    ``state`` is Specivo's: ``open``, ``draft``, ``merged`` or ``closed``.
    ``updated_at`` is the forge's update time, used to drop events that arrive
    after a newer one. ``action`` is the forge's own word (``opened``,
    ``closed``, ``merge``, ...), kept for the log only. ``external`` is true
    when the payload shows the request comes from outside: from another
    repository (a fork), or, where the forge says so, from an author who is
    not a collaborator. A payload that does not say (a repository id or,
    on GitHub, ``author_association`` missing) is external: it cannot be
    shown to come from inside.
    """

    repo_full_name: str | None
    number: int
    title: str
    body: str
    url: str
    state: str
    source_branch: str | None
    target_branch: str | None
    default_branch: str | None
    author_login: str | None
    merged_by_login: str | None
    merged_at: datetime | None
    merge_commit_sha: str | None
    updated_at: datetime | None
    action: str | None
    external: bool = False


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

    def parse_pull_request(self, payload: dict[str, Any]) -> PullRequestEvent | None:
        """Parse a pull/merge request payload; None when it names no request."""
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


def timestamp(value: Any) -> datetime | None:
    """An ISO 8601 time, or GitLab's older ``2026-10-01 10:15:00 UTC``; UTC-aware, or None."""
    if not isinstance(value, str) or not value:
        return None
    if value.endswith(" UTC"):
        value = value.removesuffix(" UTC") + "+00:00"
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
                timestamp=timestamp(item.get("timestamp")),
            )
        )
    return tuple(commits)


def str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


#: Pull request numbers are stored in an INTEGER column.
_INT_MAX = 2**31 - 1


def int_or_none(value: Any) -> int | None:
    """A positive int that fits an INTEGER column (not a bool, not a string), or None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= _INT_MAX else None


def forge_id_or_none(value: Any) -> int | None:
    """A forge's repository or project id: a positive int of any size (not a bool, not a string), or None.

    Only compared, never stored, so unlike :func:`int_or_none` it is not
    capped at the INTEGER range: GitHub's repository ids grow past it.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def sha_or_none(value: Any) -> str | None:
    sha = value.lower() if isinstance(value, str) else ""
    return sha if _SHA.fullmatch(sha) else None
