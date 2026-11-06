"""Development activity beyond commits and pull requests: CI runs, deployments, tags and releases, reviews.

Pure data and rules, no database, like :mod:`specivo.hooks.refs`. Provider
adapters parse webhook payloads into the dataclasses here; the services in
``specivo.services.repository_activity`` store them.

Statuses are Specivo's, whatever the forge calls them:

- CI runs: ``pending``, ``running``, ``success``, ``failure``, ``cancelled``,
  ``skipped``, ``neutral``;
- deployments: ``pending``, ``running``, ``success``, ``failure``,
  ``cancelled``, ``inactive`` (replaced by a newer deployment to the same
  environment);
- reviews: ``approved``, ``changes_requested``, ``commented``, ``dismissed``.

Out-of-order rule
-----------------
Forges send several events for one run or deployment and do not promise their
order. An event replaces the stored row only when its order key is at least
the stored one (:func:`ci_order_key`, :func:`deployment_order_key`): first the
attempt (a re-run is newer than anything of the run before), then the forge's
own time, then the phase (``pending`` < ``running`` < finished), so within the
same second a finished state is never undone by a late "running". An event
without a time sorts before any event with one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

# ---------------------------------------------------------------------------
# Event kinds
# ---------------------------------------------------------------------------

#: What processing a delivery does, by the kind a provider gives its event.
KIND_PUSH = "push"
KIND_PULL_REQUEST = "pull_request"
KIND_REVIEW = "review"
KIND_CI = "ci"
KIND_DEPLOYMENT = "deployment"
KIND_RELEASE = "release"
EVENT_KINDS = (KIND_PUSH, KIND_PULL_REQUEST, KIND_REVIEW, KIND_CI, KIND_DEPLOYMENT, KIND_RELEASE)

# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------

CI_PENDING = "pending"
CI_RUNNING = "running"
CI_SUCCESS = "success"
CI_FAILURE = "failure"
CI_CANCELLED = "cancelled"
CI_SKIPPED = "skipped"
CI_NEUTRAL = "neutral"
CI_STATUSES = (CI_PENDING, CI_RUNNING, CI_SUCCESS, CI_FAILURE, CI_CANCELLED, CI_SKIPPED, CI_NEUTRAL)

DEPLOY_INACTIVE = "inactive"
DEPLOYMENT_STATUSES = (CI_PENDING, CI_RUNNING, CI_SUCCESS, CI_FAILURE, CI_CANCELLED, DEPLOY_INACTIVE)

REVIEW_APPROVED = "approved"
REVIEW_CHANGES_REQUESTED = "changes_requested"
REVIEW_COMMENTED = "commented"
REVIEW_DISMISSED = "dismissed"
REVIEW_STATES = (REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED, REVIEW_COMMENTED, REVIEW_DISMISSED)

CI_RUN_TYPES = ("check_run", "check_suite", "workflow_run", "workflow_job", "pipeline", "job")

# Forge words → Specivo's CI status.
_CI_WORDS = {
    # waiting to start
    "queued": CI_PENDING,
    "waiting": CI_PENDING,
    "requested": CI_PENDING,
    "pending": CI_PENDING,
    "created": CI_PENDING,
    "waiting_for_resource": CI_PENDING,
    "preparing": CI_PENDING,
    "manual": CI_PENDING,
    "scheduled": CI_PENDING,
    "blocked": CI_PENDING,
    # running
    "in_progress": CI_RUNNING,
    "running": CI_RUNNING,
    # finished
    "success": CI_SUCCESS,
    "succeeded": CI_SUCCESS,
    "failure": CI_FAILURE,
    "failed": CI_FAILURE,
    "timed_out": CI_FAILURE,
    "startup_failure": CI_FAILURE,
    "action_required": CI_FAILURE,
    "error": CI_FAILURE,
    "cancelled": CI_CANCELLED,
    "canceled": CI_CANCELLED,
    "skipped": CI_SKIPPED,
    "neutral": CI_NEUTRAL,
    "stale": CI_NEUTRAL,
}

_DEPLOY_WORDS = {
    **{k: v for k, v in _CI_WORDS.items() if v in (CI_PENDING, CI_RUNNING, CI_SUCCESS, CI_FAILURE, CI_CANCELLED)},
    "inactive": DEPLOY_INACTIVE,
}


def ci_status(status: str | None, conclusion: str | None = None) -> str:
    """Specivo's CI status from the forge's ``status`` and, once completed, its ``conclusion``."""
    word = (status or "").lower()
    if word == "completed":
        return _CI_WORDS.get((conclusion or "").lower(), CI_NEUTRAL)
    return _CI_WORDS.get(word, CI_PENDING)


def deployment_status(state: str | None) -> str:
    return _DEPLOY_WORDS.get((state or "").lower(), CI_PENDING)


def phase(status: str) -> int:
    """0 waiting, 1 running, 2 finished."""
    if status == CI_PENDING:
        return 0
    if status == CI_RUNNING:
        return 1
    return 2


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def ci_order_key(attempt: int | None, updated_at: datetime | None, status: str) -> tuple[int, datetime, int]:
    """The order of CI events for one run: attempt, then forge time, then phase."""
    return (attempt or 0, updated_at or _EPOCH, phase(status))


def deployment_order_key(updated_at: datetime | None, status: str) -> tuple[datetime, int]:
    """The order of deployment events for one deployment: forge time, then phase.

    ``inactive`` counts as finished: a deployment is made inactive after it
    succeeded, when a newer one replaces it.
    """
    return (updated_at or _EPOCH, phase(status))


def latest_time(*values: datetime | None) -> datetime | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None


# ---------------------------------------------------------------------------
# Parsed events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CiRunEvent:
    """One CI run, job or check as one event describes it.

    ``run_type`` is the forge's object (``workflow_run``, ``check_run``,
    ``pipeline``, ...); ``external_id`` its id on the forge, unique per type.
    ``updated_at`` is the forge's latest time on the object, used for order.
    """

    run_type: str
    external_id: int
    name: str
    status: str
    head_sha: str | None
    branch: str | None
    pull_request_numbers: tuple[int, ...]
    url: str
    attempt: int
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class DeploymentEvent:
    """A deployment, or a status change of one."""

    external_id: int
    environment: str
    status: str
    sha: str | None
    ref: str | None
    url: str
    updated_at: datetime | None


@dataclass(frozen=True)
class ReleaseEvent:
    """A tag, or a release made from one.

    ``sha`` is the commit the tag points at when the payload says it (GitHub's
    ``create`` and ``release`` do not). ``commit_shas`` are the commits the
    push that created the tag listed. ``deleted`` removes the tag; a release
    event with ``is_release`` false and not deleted (a draft or a deleted
    release) leaves only the tag.
    """

    tag_name: str
    name: str
    sha: str | None
    url: str
    released_at: datetime | None
    is_release: bool
    deleted: bool = False
    release_deleted: bool = False
    commit_shas: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewEvent:
    """One reviewer's verdict on a pull/merge request; ``state`` None withdraws it (GitLab unapproval)."""

    number: int
    reviewer_login: str
    state: str | None
    submitted_at: datetime | None
    url: str = ""


# ---------------------------------------------------------------------------
# Reviews: latest wins, but a comment does not withdraw a verdict
# ---------------------------------------------------------------------------


def review_replaces(
    stored_state: str | None, stored_at: datetime | None, new_state: str | None, new_at: datetime | None
) -> bool:
    """Whether a reviewer's new review replaces the stored one.

    The newer one wins (an event without a time only replaces one without a
    time). Like the forges show it, a plain comment after an approval or a
    change request does not withdraw it.
    """
    if stored_state is None:
        return True
    if stored_at is not None and (new_at is None or new_at < stored_at):
        return False
    return not (new_state == REVIEW_COMMENTED and stored_state in (REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED))


# ---------------------------------------------------------------------------
# Environment types
# ---------------------------------------------------------------------------

EnvironmentType = Literal["development", "testing", "staging", "production"]
ENV_DEVELOPMENT: EnvironmentType = "development"
ENV_TESTING: EnvironmentType = "testing"
ENV_STAGING: EnvironmentType = "staging"
ENV_PRODUCTION: EnvironmentType = "production"
ENVIRONMENT_TYPES: tuple[EnvironmentType, ...] = (ENV_DEVELOPMENT, ENV_TESTING, ENV_STAGING, ENV_PRODUCTION)

_TOKEN = re.compile(r"[a-z0-9]+")
# Checked in this order; the first type with a matching word wins, so "preprod" is staging, not production.
_ENV_WORDS: tuple[tuple[EnvironmentType, frozenset[str]], ...] = (
    (ENV_STAGING, frozenset({"staging", "stage", "stg", "preprod", "preproduction", "prestaging"})),
    (ENV_PRODUCTION, frozenset({"production", "prod", "prd", "live"})),
    (ENV_TESTING, frozenset({"test", "testing", "tests", "qa", "uat", "sit"})),
    (ENV_DEVELOPMENT, frozenset({"dev", "develop", "development", "review", "local", "sandbox", "preview"})),
)


def guess_environment_type(name: str) -> EnvironmentType | None:
    """The environment type a deployment environment's name suggests, or None (unclassified).

    The name is split into lower-case words (``production-eu`` → ``production``,
    ``eu``), and ``pre-prod`` / ``pre_production`` read as one word first.
    """
    lowered = name.lower()
    lowered = re.sub(r"\bpre[-_ .]?prod", "preprod", lowered)
    words = set(_TOKEN.findall(lowered))
    for env_type, known in _ENV_WORDS:
        if words & known:
            return env_type
    return None


def environment_type(name: str, overrides: Mapping[str, str] | None) -> EnvironmentType | None:
    """The type of environment *name*: the repository's override first, else the guess.

    Overrides match the name without regard to case; one ending in ``*``
    matches every name that starts with the rest (``review/*``). An exact
    override wins over a pattern, and a longer pattern over a shorter one.
    """
    if overrides:
        lowered = name.lower()
        exact = {k.lower(): v for k, v in overrides.items() if not k.endswith("*")}
        if lowered in exact and exact[lowered] in ENVIRONMENT_TYPES:
            return exact[lowered]  # type: ignore[return-value]
        patterns = sorted(
            ((k[:-1].lower(), v) for k, v in overrides.items() if k.endswith("*")), key=lambda p: -len(p[0])
        )
        for prefix, value in patterns:
            if lowered.startswith(prefix) and value in ENVIRONMENT_TYPES:
                return value  # type: ignore[return-value]
    return guess_environment_type(name)


# ---------------------------------------------------------------------------
# Combined statuses for the summary
# ---------------------------------------------------------------------------


def combined_pull_request_state(states: Iterable[str]) -> str | None:
    """``open`` if any is open (a draft counts), else ``merged`` if any is, else ``closed``; None without any."""
    seen = set(states)
    if not seen:
        return None
    if seen & {"open", "draft"}:
        return "open"
    if "merged" in seen:
        return "merged"
    return "closed"


def combined_review_state(states: Iterable[str]) -> str | None:
    """``changes_requested`` if any reviewer asks for changes, else ``approved``, else ``commented``."""
    seen = set(states) - {REVIEW_DISMISSED}
    for state in (REVIEW_CHANGES_REQUESTED, REVIEW_APPROVED, REVIEW_COMMENTED):
        if state in seen:
            return state
    return None


@dataclass(frozen=True)
class CiLatest:
    """What :func:`combined_ci_state` needs of a run."""

    group: tuple[object, ...]
    status: str
    order: tuple[int, datetime, int]


def combined_ci_state(runs: Iterable[CiLatest]) -> str | None:
    """``failing`` if the latest run of any group failed, else ``running``, else ``passing``, else ``neutral``.

    A group is one check or workflow (repository, type and name): only its
    latest run counts, so a fixed build is passing again.
    """
    latest: dict[tuple[object, ...], CiLatest] = {}
    for run in runs:
        current = latest.get(run.group)
        if current is None or run.order >= current.order:
            latest[run.group] = run
    if not latest:
        return None
    statuses = {run.status for run in latest.values()}
    if CI_FAILURE in statuses:
        return "failing"
    if statuses & {CI_PENDING, CI_RUNNING}:
        return "running"
    if CI_SUCCESS in statuses:
        return "passing"
    return "neutral"
