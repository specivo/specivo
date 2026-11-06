"""GitHub, and the GitHub-shaped parser that Gitea and Forgejo share.

GitHub signs the raw body with HMAC-SHA256 and sends ``sha256=<hex>`` in
``X-Hub-Signature-256``; the event is in ``X-GitHub-Event`` and the delivery
id in ``X-GitHub-Delivery``.
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
    hmac_sha256_hex,
    int_or_none,
    is_null_sha,
    list_at,
    parse_commits,
    sha_or_none,
    str_or_none,
    timestamp,
    web_url,
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
            after=None
            if payload.get("deleted") is True or is_null_sha(payload.get("after"))
            else sha_or_none(payload.get("after")),
            deleted=payload.get("deleted") is True or is_null_sha(payload.get("after")),
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

    # -- events beyond push and pull request ---------------------------------

    _KINDS = {
        "push": dev.KIND_PUSH,
        "pull_request": dev.KIND_PULL_REQUEST,
        "pull_request_review": dev.KIND_REVIEW,
        "check_run": dev.KIND_CI,
        "check_suite": dev.KIND_CI,
        "workflow_run": dev.KIND_CI,
        "workflow_job": dev.KIND_CI,
        "deployment": dev.KIND_DEPLOYMENT,
        "deployment_status": dev.KIND_DEPLOYMENT,
        "create": dev.KIND_RELEASE,
        "release": dev.KIND_RELEASE,
    }

    def event_kind(self, event: str) -> str | None:
        return self._KINDS.get(event)

    def parse_ci(self, event: str, payload: dict[str, Any]) -> CiRunEvent | None:
        obj = dict_at(payload, event)
        external_id = forge_id_or_none(obj.get("id"))
        if event not in ("check_run", "check_suite", "workflow_run", "workflow_job") or external_id is None:
            return None
        suite = dict_at(obj, "check_suite")
        if event == "check_suite":
            name = str_or_none(dict_at(obj, "app").get("name")) or "Check suite"
        elif event == "workflow_job":
            name = str_or_none(obj.get("name")) or "Job"
            workflow = str_or_none(obj.get("workflow_name"))
            name = f"{workflow} / {name}" if workflow else name
        else:
            name = str_or_none(obj.get("name")) or event.replace("_", " ")
        started = timestamp(obj.get("started_at")) or timestamp(obj.get("run_started_at"))
        completed = timestamp(obj.get("completed_at"))
        updated = timestamp(obj.get("updated_at"))
        numbers = tuple(
            n
            for item in list_at(obj, "pull_requests")
            if isinstance(item, dict) and (n := int_or_none(item.get("number"))) is not None
        )
        return CiRunEvent(
            run_type=event,
            external_id=external_id,
            name=name[:255],
            status=dev.ci_status(str_or_none(obj.get("status")), str_or_none(obj.get("conclusion"))),
            head_sha=sha_or_none(obj.get("head_sha")),
            branch=cut_text(str_or_none(obj.get("head_branch")) or str_or_none(suite.get("head_branch")), 255) or None,
            pull_request_numbers=numbers[:20],
            url=web_url(obj.get("html_url")) or web_url(obj.get("details_url")),
            attempt=int_or_none(obj.get("run_attempt")) or 1,
            started_at=started,
            completed_at=completed,
            updated_at=dev.latest_time(updated, completed, started, timestamp(obj.get("created_at"))),
        )

    def parse_deployment(self, event: str, payload: dict[str, Any]) -> DeploymentEvent | None:
        deployment = dict_at(payload, "deployment")
        external_id = forge_id_or_none(deployment.get("id"))
        if external_id is None:
            return None
        status_obj = dict_at(payload, "deployment_status")
        if event == "deployment_status" and status_obj:
            status = dev.deployment_status(str_or_none(status_obj.get("state")))
            updated = dev.latest_time(timestamp(status_obj.get("updated_at")), timestamp(status_obj.get("created_at")))
            url = (
                web_url(status_obj.get("environment_url"))
                or web_url(status_obj.get("log_url"))
                or web_url(status_obj.get("target_url"))
            )
        else:
            status = dev.CI_PENDING
            updated = dev.latest_time(timestamp(deployment.get("updated_at")), timestamp(deployment.get("created_at")))
            url = ""
        environment = (
            str_or_none(status_obj.get("environment"))
            or str_or_none(deployment.get("environment"))
            or str_or_none(deployment.get("original_environment"))
        )
        if environment is None:
            return None
        return DeploymentEvent(
            external_id=external_id,
            environment=environment[:255],
            status=status,
            sha=sha_or_none(deployment.get("sha")),
            ref=cut_text(deployment.get("ref"), 255) or None,
            url=url,
            updated_at=updated,
        )

    def parse_release(self, event: str, payload: dict[str, Any]) -> ReleaseEvent | None:
        if event == "create":
            # A new branch is not a release; a tag is (GitHub sends no SHA here, Gitea and Forgejo do).
            tag = str_or_none(payload.get("ref"))
            if payload.get("ref_type") != "tag" or tag is None:
                return None
            return ReleaseEvent(
                tag_name=tag.removeprefix("refs/tags/")[:255],
                name="",
                sha=sha_or_none(payload.get("sha")),
                url="",
                released_at=None,
                is_release=False,
            )
        if event != "release":
            return None
        release = dict_at(payload, "release")
        tag = str_or_none(release.get("tag_name"))
        if tag is None:
            return None
        action = str_or_none(payload.get("action")) or ""
        deleted = action in ("deleted", "unpublished")
        published = release.get("draft") is not True and not deleted
        commitish = sha_or_none(release.get("target_commitish"))
        return ReleaseEvent(
            tag_name=tag[:255],
            name=cut_text(release.get("name"), 255),
            sha=commitish,
            url=web_url(release.get("html_url")),
            released_at=timestamp(release.get("published_at")) or timestamp(release.get("created_at")),
            is_release=published,
            release_deleted=deleted,
        )

    _GITEA_REVIEW_EVENTS = {
        "pull_request_review_approved": dev.REVIEW_APPROVED,
        "pull_request_review_rejected": dev.REVIEW_CHANGES_REQUESTED,
        "pull_request_review_comment": dev.REVIEW_COMMENTED,
        "pull_request_approved": dev.REVIEW_APPROVED,
        "pull_request_rejected": dev.REVIEW_CHANGES_REQUESTED,
        "pull_request_comment": dev.REVIEW_COMMENTED,
    }

    def parse_reviews(self, event: str, payload: dict[str, Any]) -> list[ReviewEvent]:
        pr = dict_at(payload, "pull_request")
        number = int_or_none(pr.get("number")) or int_or_none(payload.get("number"))
        if number is None:
            return []
        if event in self._GITEA_REVIEW_EVENTS:
            # Gitea and Forgejo: the event names the verdict, the sender is the reviewer.
            reviewer = _login(dict_at(payload, "sender"))
            if reviewer is None:
                return []
            return [
                ReviewEvent(
                    number=number,
                    reviewer_login=reviewer[:255],
                    state=self._GITEA_REVIEW_EVENTS[event],
                    submitted_at=timestamp(pr.get("updated_at")),
                    url=web_url(pr.get("html_url")),
                )
            ]
        if event != "pull_request_review":
            return []
        review = dict_at(payload, "review")
        reviewer = _login(dict_at(review, "user"))
        state = (str_or_none(review.get("state")) or "").lower()
        if payload.get("action") == "dismissed":
            state = dev.REVIEW_DISMISSED
        if reviewer is None or state not in dev.REVIEW_STATES:
            return []
        return [
            ReviewEvent(
                number=number,
                reviewer_login=reviewer[:255],
                state=state,
                submitted_at=timestamp(review.get("submitted_at")),
                url=web_url(review.get("html_url")),
            )
        ]


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
