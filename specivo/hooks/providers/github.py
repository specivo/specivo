"""GitHub, and the GitHub-shaped parser that Gitea and Forgejo share.

GitHub signs the raw body with HMAC-SHA256 and sends ``sha256=<hex>`` in
``X-Hub-Signature-256``; the event is in ``X-GitHub-Event`` and the delivery
id in ``X-GitHub-Delivery``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from specivo.hooks.providers.base import (
    PR_BODY_MAX_LENGTH,
    PR_TITLE_MAX_LENGTH,
    PullRequestEvent,
    PushEvent,
    SignatureError,
    branch_of,
    constant_time_equal,
    delivery_id_from,
    dict_at,
    forge_id_or_none,
    header,
    hmac_sha256_hex,
    int_or_none,
    parse_commits,
    sha_or_none,
    str_or_none,
    timestamp,
)

#: GitHub's ``author_association`` values for people with a role in the repository.
_COLLABORATOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def _login(user: dict[str, Any]) -> str | None:
    return str_or_none(user.get("login")) or str_or_none(user.get("username"))


def _repo_id(side: Any) -> int | None:
    """The repository id of a ``head`` or ``base``: Gitea and Forgejo send ``repo_id``, all send ``repo.id``."""
    if not isinstance(side, dict):
        return None
    return forge_id_or_none(side.get("repo_id")) or forge_id_or_none(dict_at(side, "repo").get("id"))


def _external(pr: dict[str, Any], *, needs_author_association: bool) -> bool:
    """From a fork, or by an author without a role, or the payload cannot show otherwise.

    The head and base repositories must both be named and be the same one;
    a missing id (a deleted fork sends ``head.repo: null``) counts as a fork.
    ``author_association`` is judged where present; only GitHub sends it, so
    only there does its absence make the request external.
    """
    head_id, base_id = _repo_id(pr.get("head")), _repo_id(pr.get("base"))
    if head_id is None or base_id is None or head_id != base_id:
        return True
    association = str_or_none(pr.get("author_association"))
    if association is None:
        return needs_author_association
    return association.upper() not in _COLLABORATOR_ASSOCIATIONS


class GitHubShapedPayloads:
    """Payload parsing for forges whose push and pull request payloads follow GitHub's.

    A pull request is merged when ``merged`` is true, or when it is closed
    with a ``merged_at``; closed otherwise; open, or draft with ``draft``.
    """

    #: Whether the forge always sends ``author_association`` (GitHub does; Gitea and Forgejo never do).
    sends_author_association = False

    def repo_full_name(self, payload: dict[str, Any]) -> str | None:
        return str_or_none(dict_at(payload, "repository").get("full_name"))

    def parse_push(self, payload: dict[str, Any]) -> PushEvent:
        ref = str_or_none(payload.get("ref")) or ""
        pusher = dict_at(payload, "pusher")
        sender = dict_at(payload, "sender")
        return PushEvent(
            repo_full_name=self.repo_full_name(payload),
            ref=ref,
            branch=branch_of(ref),
            default_branch=str_or_none(dict_at(payload, "repository").get("default_branch")),
            commits=parse_commits(payload.get("commits")),
            # GitHub's pusher has ``name``; Gitea and Forgejo send ``login``/``username``.
            pusher_login=str_or_none(pusher.get("login"))
            or str_or_none(pusher.get("username"))
            or str_or_none(sender.get("login"))
            or str_or_none(pusher.get("name")),
        )

    def parse_pull_request(self, payload: dict[str, Any]) -> PullRequestEvent | None:
        pr = dict_at(payload, "pull_request")
        number = int_or_none(pr.get("number")) or int_or_none(payload.get("number"))
        if number is None:
            return None
        merged_at = timestamp(pr.get("merged_at"))
        if pr.get("merged") is True or (pr.get("state") == "closed" and merged_at is not None):
            state = "merged"
        elif pr.get("state") == "closed":
            state = "closed"
        else:
            state = "draft" if pr.get("draft") is True else "open"
        return PullRequestEvent(
            repo_full_name=self.repo_full_name(payload),
            number=number,
            title=(str_or_none(pr.get("title")) or "")[:PR_TITLE_MAX_LENGTH],
            body=(str_or_none(pr.get("body")) or "")[:PR_BODY_MAX_LENGTH],
            url=str_or_none(pr.get("html_url")) or "",
            state=state,
            source_branch=str_or_none(dict_at(pr, "head").get("ref")),
            target_branch=str_or_none(dict_at(pr, "base").get("ref")),
            default_branch=str_or_none(dict_at(payload, "repository").get("default_branch")),
            author_login=_login(dict_at(pr, "user")),
            merged_by_login=_login(dict_at(pr, "merged_by")) if state == "merged" else None,
            merged_at=merged_at if state == "merged" else None,
            merge_commit_sha=sha_or_none(pr.get("merge_commit_sha")) if state == "merged" else None,
            updated_at=timestamp(pr.get("updated_at")),
            action=str_or_none(payload.get("action")),
            external=_external(pr, needs_author_association=self.sends_author_association),
        )


class GitHubProvider(GitHubShapedPayloads):
    name = "github"
    sends_author_association = True

    def verify(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> None:
        given = header(headers, "X-Hub-Signature-256")
        if not given:
            raise SignatureError("missing X-Hub-Signature-256")
        if not constant_time_equal(given, "sha256=" + hmac_sha256_hex(secret, raw_body)):
            raise SignatureError("X-Hub-Signature-256 does not match")

    def event_type(self, headers: Mapping[str, str]) -> str | None:
        return (header(headers, "X-GitHub-Event") or "").strip().lower() or None

    def delivery_id(self, headers: Mapping[str, str], raw_body: bytes) -> str:
        return delivery_id_from(headers, ("X-GitHub-Delivery",), raw_body)
