"""GitLab.

GitLab does not sign: it sends the shared secret as a plain token in
``X-Gitlab-Token``, compared in constant time. The event is in
``X-Gitlab-Event`` (``Push Hook``, ``Merge Request Hook``, ...). Recent
versions send a delivery id in ``X-Gitlab-Event-UUID`` and ``Idempotency-Key``;
older ones send none, and the body hash stands in.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from specivo.hooks.providers.base import (
    PushEvent,
    SignatureError,
    branch_of,
    constant_time_equal,
    delivery_id_from,
    dict_at,
    header,
    parse_commits,
    str_or_none,
)

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
