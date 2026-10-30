"""GitHub, and the GitHub-shaped parser that Gitea and Forgejo share.

GitHub signs the raw body with HMAC-SHA256 and sends ``sha256=<hex>`` in
``X-Hub-Signature-256``; the event is in ``X-GitHub-Event`` and the delivery
id in ``X-GitHub-Delivery``.
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
    hmac_sha256_hex,
    parse_commits,
    str_or_none,
)


class GitHubShapedPayloads:
    """Payload parsing for forges whose push payload follows GitHub's."""

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


class GitHubProvider(GitHubShapedPayloads):
    name = "github"

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
