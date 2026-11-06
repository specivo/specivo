"""Gitea and Forgejo: GitHub-shaped payloads with their own headers.

Both sign the raw body with HMAC-SHA256 and send it as **bare hex** (no
``sha256=`` prefix) in ``X-Gitea-Signature``; Forgejo sends the same value in
``X-Forgejo-Signature`` as well. Every signature header present must match,
and at least one must be there. The event comes from the forge's own header
first, then ``X-Gogs-Event`` and ``X-GitHub-Event``, which both also send.
"""

from __future__ import annotations

from collections.abc import Mapping

from specivo.hooks import development as dev
from specivo.hooks.providers.base import (
    SignatureError,
    constant_time_equal,
    delivery_id_from,
    header,
    hmac_sha256_hex,
)
from specivo.hooks.providers.github import GitHubShapedPayloads

_SIGNATURE_HEADERS = ("X-Forgejo-Signature", "X-Gitea-Signature")


class _GiteaFamilyProvider(GitHubShapedPayloads):
    """Gitea and Forgejo send no deployments; their Actions events follow GitHub's ``workflow_*``."""

    name: str
    _event_headers: tuple[str, ...]
    _delivery_headers: tuple[str, ...]
    _KINDS = {
        "push": dev.KIND_PUSH,
        "pull_request": dev.KIND_PULL_REQUEST,
        "pull_request_review_approved": dev.KIND_REVIEW,
        "pull_request_review_rejected": dev.KIND_REVIEW,
        "pull_request_review_comment": dev.KIND_REVIEW,
        # What X-Gitea-Event says for reviews; X-Gitea-Event-Type carries the longer names above.
        "pull_request_approved": dev.KIND_REVIEW,
        "pull_request_rejected": dev.KIND_REVIEW,
        "pull_request_comment": dev.KIND_REVIEW,
        "workflow_run": dev.KIND_CI,
        "workflow_job": dev.KIND_CI,
        "create": dev.KIND_RELEASE,
        "release": dev.KIND_RELEASE,
    }

    def verify(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> None:
        given = [value for name in _SIGNATURE_HEADERS if (value := header(headers, name))]
        if not given:
            raise SignatureError("missing X-Gitea-Signature / X-Forgejo-Signature")
        expected = hmac_sha256_hex(secret, raw_body)
        # Compare every header, no early exit, so the time taken does not say which one failed.
        matches = [constant_time_equal(value, expected) for value in given]
        if not all(matches):
            raise SignatureError("signature does not match")

    def event_type(self, headers: Mapping[str, str]) -> str | None:
        for name in self._event_headers:
            value = (header(headers, name) or "").strip().lower()
            if value:
                return value
        return None

    def delivery_id(self, headers: Mapping[str, str], raw_body: bytes) -> str:
        return delivery_id_from(headers, self._delivery_headers, raw_body)


class GiteaProvider(_GiteaFamilyProvider):
    name = "gitea"
    _event_headers = ("X-Gitea-Event", "X-Gogs-Event", "X-GitHub-Event")
    _delivery_headers = ("X-Gitea-Delivery", "X-Gogs-Delivery", "X-GitHub-Delivery")


class ForgejoProvider(_GiteaFamilyProvider):
    name = "forgejo"
    _event_headers = ("X-Forgejo-Event", "X-Gitea-Event", "X-Gogs-Event", "X-GitHub-Event")
    _delivery_headers = ("X-Forgejo-Delivery", "X-Gitea-Delivery", "X-Gogs-Delivery", "X-GitHub-Delivery")
