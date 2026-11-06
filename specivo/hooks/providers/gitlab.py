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

from specivo.hooks import development as dev
from specivo.hooks.development import CiRunEvent, DeploymentEvent, ReleaseEvent, ReviewEvent
from specivo.hooks.providers.base import (
    PR_BODY_MAX_LENGTH,
    PR_TITLE_MAX_LENGTH,
    PullRequestEvent,
    PushEvent,
    SignatureError,
    branch_of,
    constant_time_equal,
    cut_text,
    delivery_id_from,
    dict_at,
    forge_id_or_none,
    header,
    int_or_none,
    is_null_sha,
    list_at,
    parse_commits,
    sha_or_none,
    str_or_none,
    timestamp,
    web_url,
)

_STATES = {"opened": "open", "locked": "open", "merged": "merged", "closed": "closed"}

_EVENTS = {
    "push hook": "push",
    "merge request hook": "pull_request",
    # Older versions call job events "Build Hook".
    "build hook": "job",
}

_KINDS = {
    "push": dev.KIND_PUSH,
    # A tag push is a push of refs/tags/...; processing turns it into a tag.
    "tag_push": dev.KIND_PUSH,
    "pull_request": dev.KIND_PULL_REQUEST,
    "pipeline": dev.KIND_CI,
    "job": dev.KIND_CI,
    "deployment": dev.KIND_DEPLOYMENT,
    "release": dev.KIND_RELEASE,
}

# Merge request actions that are one reviewer's approval, or its withdrawal.
_APPROVAL_ACTIONS = {"approved": dev.REVIEW_APPROVED, "approval": dev.REVIEW_APPROVED}
_UNAPPROVAL_ACTIONS = frozenset({"unapproved", "unapproval"})
# ``reviewers[].state`` in recent versions.
_REVIEWER_STATES = {
    "approved": dev.REVIEW_APPROVED,
    "requested_changes": dev.REVIEW_CHANGES_REQUESTED,
    "reviewed": dev.REVIEW_COMMENTED,
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
            after=None
            if is_null_sha(payload.get("after"))
            else sha_or_none(payload.get("checkout_sha")) or sha_or_none(payload.get("after")),
            deleted=is_null_sha(payload.get("after")),
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
        source, target = forge_id_or_none(mr.get("source_project_id")), forge_id_or_none(mr.get("target_project_id"))
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
            # A merge request from a fork comes from another project; one that names no project
            # cannot be shown to come from inside.
            external=source is None or target is None or source != target,
        )

    # -- events beyond push and merge request --------------------------------

    def event_kind(self, event: str) -> str | None:
        return _KINDS.get(event)

    def _web_url(self, payload: dict[str, Any]) -> str:
        return (
            web_url(dict_at(payload, "project").get("web_url"))
            or web_url(dict_at(payload, "repository").get("homepage"))
        ).rstrip("/")

    def parse_ci(self, event: str, payload: dict[str, Any]) -> CiRunEvent | None:
        if event == "pipeline":
            attrs = dict_at(payload, "object_attributes")
            external_id = forge_id_or_none(attrs.get("id"))
            if external_id is None:
                return None
            builds = list_at(payload, "builds")
            build_times = [
                timestamp(b.get(k)) for b in builds if isinstance(b, dict) for k in ("started_at", "finished_at")
            ]
            mr_number = int_or_none(dict_at(payload, "merge_request").get("iid"))
            base = self._web_url(payload)
            return CiRunEvent(
                run_type="pipeline",
                external_id=external_id,
                name=cut_text(str_or_none(attrs.get("name")) or "Pipeline", 255),
                status=dev.ci_status(str_or_none(attrs.get("status"))),
                head_sha=sha_or_none(attrs.get("sha")),
                branch=None if attrs.get("tag") is True else (cut_text(attrs.get("ref"), 255) or None),
                pull_request_numbers=(mr_number,) if mr_number else (),
                url=web_url(attrs.get("url")) or (f"{base}/-/pipelines/{external_id}" if base else ""),
                attempt=1,
                started_at=timestamp(attrs.get("created_at")),
                completed_at=timestamp(attrs.get("finished_at")),
                updated_at=dev.latest_time(
                    timestamp(attrs.get("created_at")), timestamp(attrs.get("finished_at")), *build_times
                ),
            )
        if event == "job":
            external_id = forge_id_or_none(payload.get("build_id"))
            if external_id is None:
                return None
            started, finished = timestamp(payload.get("build_started_at")), timestamp(payload.get("build_finished_at"))
            base = self._web_url(payload)
            stage = str_or_none(payload.get("build_stage"))
            name = str_or_none(payload.get("build_name")) or "Job"
            return CiRunEvent(
                run_type="job",
                external_id=external_id,
                name=(f"{stage} / {name}" if stage else name)[:255],
                status=dev.ci_status(str_or_none(payload.get("build_status"))),
                head_sha=sha_or_none(payload.get("sha")),
                branch=None if payload.get("tag") is True else (cut_text(payload.get("ref"), 255) or None),
                pull_request_numbers=(),
                url=f"{base}/-/jobs/{external_id}" if base else "",
                # A retried job is a new job with its own id.
                attempt=1,
                started_at=started,
                completed_at=finished,
                updated_at=dev.latest_time(timestamp(payload.get("build_created_at")), started, finished),
            )
        return None

    def parse_deployment(self, event: str, payload: dict[str, Any]) -> DeploymentEvent | None:
        external_id = forge_id_or_none(payload.get("deployment_id"))
        environment = str_or_none(payload.get("environment"))
        if event != "deployment" or external_id is None or environment is None:
            return None
        return DeploymentEvent(
            external_id=external_id,
            environment=environment[:255],
            status=dev.deployment_status(str_or_none(payload.get("status"))),
            sha=sha_or_none(payload.get("sha")),
            ref=cut_text(payload.get("ref"), 255) or None,
            url=web_url(payload.get("environment_external_url")) or web_url(payload.get("deployable_url")),
            updated_at=timestamp(payload.get("status_changed_at")),
        )

    def parse_release(self, event: str, payload: dict[str, Any]) -> ReleaseEvent | None:
        tag = str_or_none(payload.get("tag"))
        if event != "release" or tag is None:
            return None
        action = str_or_none(payload.get("action")) or ""
        return ReleaseEvent(
            tag_name=tag[:255],
            name=cut_text(payload.get("name"), 255),
            sha=sha_or_none(dict_at(payload, "commit").get("id")),
            url=web_url(payload.get("url")),
            released_at=timestamp(payload.get("released_at")) or timestamp(payload.get("created_at")),
            is_release=action != "delete",
            release_deleted=action == "delete",
        )

    def parse_reviews(self, event: str, payload: dict[str, Any]) -> list[ReviewEvent]:
        if event != "pull_request":
            return []
        attrs = dict_at(payload, "object_attributes")
        number = int_or_none(attrs.get("iid"))
        if number is None:
            return []
        at = timestamp(attrs.get("updated_at"))
        url = web_url(attrs.get("url"))
        reviews: dict[str, ReviewEvent] = {}
        for reviewer in list_at(payload, "reviewers"):
            if not isinstance(reviewer, dict):
                continue
            login = str_or_none(reviewer.get("username"))
            state = _REVIEWER_STATES.get(str_or_none(reviewer.get("state")) or "")
            if login and state:
                reviews[login] = ReviewEvent(number, login[:255], state, at, url)
        action = str_or_none(attrs.get("action")) or ""
        user = str_or_none(dict_at(payload, "user").get("username"))
        if user and action in _APPROVAL_ACTIONS:
            reviews[user] = ReviewEvent(number, user[:255], _APPROVAL_ACTIONS[action], at, url)
        elif user and action in _UNAPPROVAL_ACTIONS:
            reviews[user] = ReviewEvent(number, user[:255], None, at, url)
        return list(reviews.values())[:50]
