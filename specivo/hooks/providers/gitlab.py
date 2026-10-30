"""GitLab.

GitLab does not sign: it sends the shared secret as a plain token in
``X-Gitlab-Token``, compared in constant time. The event is in
``X-Gitlab-Event`` (``Push Hook``, ``Merge Request Hook``, ...). Recent
versions send a delivery id in ``X-Gitlab-Event-UUID`` and ``Idempotency-Key``;
older ones send none, and the body hash stands in.

A merge request's ``state`` is ``opened``, ``merged``, ``closed`` or
``locked`` (open, discussion locked); ``draft`` or the older
``work_in_progress`` makes an open one a draft. Older versions write times as
``2026-10-01 10:15:00 UTC``. The payload names the user who triggered the
event, not the author: the author is taken from the event that opened it.
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
    header,
    int_or_none,
    parse_commits,
    sha_or_none,
    str_or_none,
    timestamp,
)

_STATES = {"opened": "open", "locked": "open", "merged": "merged", "closed": "closed"}

_EVENTS = {
    "push hook": "push",
    "merge request hook": "pull_request",
}


class GitLabProvider:
    name = "gitlab"

    def verify(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> None:
        given = header(headers, "X-Gitlab-Token")
        if not given:
            raise SignatureError("missing X-Gitlab-Token")
        if not constant_time_equal(given, secret):
            raise SignatureError("X-Gitlab-Token does not match")

    def event_type(self, headers: Mapping[str, str]) -> str | None:
        raw = (header(headers, "X-Gitlab-Event") or "").strip().lower()
        if not raw:
            return None
        # Other events keep their name in snake case ("Tag Push Hook" -> "tag_push").
        return _EVENTS.get(raw) or raw.removesuffix(" hook").replace(" ", "_")

    def delivery_id(self, headers: Mapping[str, str], raw_body: bytes) -> str:
        return delivery_id_from(headers, ("X-Gitlab-Event-UUID", "Idempotency-Key"), raw_body)

    def repo_full_name(self, payload: dict[str, Any]) -> str | None:
        return str_or_none(dict_at(payload, "project").get("path_with_namespace"))

    def parse_push(self, payload: dict[str, Any]) -> PushEvent:
        ref = str_or_none(payload.get("ref")) or ""
        return PushEvent(
            repo_full_name=self.repo_full_name(payload),
            ref=ref,
            branch=branch_of(ref),
            default_branch=str_or_none(dict_at(payload, "project").get("default_branch")),
            commits=parse_commits(payload.get("commits")),
            pusher_login=str_or_none(payload.get("user_username")),
        )

    def parse_pull_request(self, payload: dict[str, Any]) -> PullRequestEvent | None:
        mr = dict_at(payload, "object_attributes")
        number = int_or_none(mr.get("iid"))
        if number is None:
            return None
        state = _STATES.get(str_or_none(mr.get("state")) or "", "open")
        if state == "open" and (mr.get("draft") is True or mr.get("work_in_progress") is True):
            state = "draft"
        action = str_or_none(mr.get("action"))
        user = str_or_none(dict_at(payload, "user").get("username"))
        merged = state == "merged"
        return PullRequestEvent(
            repo_full_name=self.repo_full_name(payload),
            number=number,
            title=(str_or_none(mr.get("title")) or "")[:PR_TITLE_MAX_LENGTH],
            body=(str_or_none(mr.get("description")) or "")[:PR_BODY_MAX_LENGTH],
            url=str_or_none(mr.get("url")) or "",
            state=state,
            source_branch=str_or_none(mr.get("source_branch")),
            target_branch=str_or_none(mr.get("target_branch")),
            default_branch=str_or_none(dict_at(payload, "project").get("default_branch")),
            author_login=user if action == "open" else None,
            merged_by_login=user if merged and action == "merge" else None,
            merged_at=(timestamp(mr.get("merged_at")) or timestamp(mr.get("updated_at"))) if merged else None,
            merge_commit_sha=sha_or_none(mr.get("merge_commit_sha")) if merged else None,
            updated_at=timestamp(mr.get("updated_at")),
            action=action,
        )
