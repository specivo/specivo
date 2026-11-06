"""Pydantic schemas for repository integration endpoints."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from specivo.hooks.development import EnvironmentType
from specivo.hooks.repo_url import is_ssh_address
from specivo.schemas.common import PaginatedResponse

Provider = Literal["github", "gitlab", "gitea", "forgejo"]
Scope = Literal["project", "project_and_subprojects"]

# ``owner/repo``; GitLab nests groups, so any number of ``/`` segments.
_REPO_FULL_NAME = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+")


def _validate_repo_full_name(v: str) -> str:
    v = v.strip().strip("/")
    if len(v) > 255 or not _REPO_FULL_NAME.fullmatch(v) or any(part in (".", "..") for part in v.split("/")):
        raise ValueError("repo_full_name must look like owner/repository")
    return v


def _validate_repo_url(v: str) -> str:
    v = v.strip()
    if len(v) > 2048:
        raise ValueError("repo_url must be 2048 characters or fewer")
    if is_ssh_address(v):
        # Converted to the repository's web URL when the integration is saved.
        return v
    parsed = urlparse(v)
    # Shown as a link in the UI: only web URLs, never javascript: or data:.
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("repo_url must be an http or https URL")
    return v


class RepositorySettings(BaseModel):
    """What the repository may change on issues; everything is off by default.

    - ``commands_enabled``: commit messages may move issues (``Fixes KEY-1``
      on the default branch, to ``fixing_status_id``) as the commit's matched
      user. ``time_logging_enabled`` also lets ``KEY-1 @2h`` log time.
    - ``merge_automation_enabled``: a merged pull request with a closing
      reference moves the issue to ``merge_status_id``, as ``$git``.

    ``ignore_external_pull_requests`` is the exception: on by default, it
    skips pull request events from forks and from authors who are not
    collaborators, except the merge itself.

    ``environment_types`` maps deployment environment names to a type
    (``development``, ``testing``, ``staging``, ``production``), overriding
    the guess from the name. Names match without regard to case; a name
    ending in ``*`` matches every environment that starts with the rest
    (``review/*``). At most 100 entries.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    commands_enabled: bool = False
    time_logging_enabled: bool = False
    fixing_status_id: int | None = Field(default=None, gt=0)
    merge_status_id: int | None = Field(default=None, gt=0)
    merge_automation_enabled: bool = False
    ignore_external_pull_requests: bool = True
    environment_types: dict[str, EnvironmentType] = Field(default_factory=dict)

    @field_validator("environment_types")
    @classmethod
    def _check_environment_types(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 100:
            raise ValueError("environment_types holds at most 100 entries")
        cleaned: dict[str, str] = {}
        for name, env_type in value.items():
            name = name.strip()
            if not name or len(name) > 255 or name == "*":
                raise ValueError("environment names must be 1 to 255 characters (and not just '*')")
            if name.lower() in {k.lower() for k in cleaned}:
                raise ValueError(f"environment {name!r} is listed twice")
            cleaned[name] = env_type
        return cleaned

    @model_validator(mode="after")
    def _check_dependencies(self) -> RepositorySettings:
        if self.time_logging_enabled and not self.commands_enabled:
            raise ValueError("time_logging_enabled needs commands_enabled")
        if self.merge_automation_enabled and self.merge_status_id is None:
            raise ValueError("merge_automation_enabled needs merge_status_id")
        return self


class RepositoryIntegrationCreate(BaseModel):
    """A new integration. The secret is always generated and returned once.

    ``repo_url`` may be a web URL or an SSH address; it is stored as the
    repository's web URL. Provider, URL and repository must agree (see
    ``specivo.hooks.repo_url``), else 422 naming the field at fault.
    """

    provider: Provider
    repo_full_name: str
    repo_url: str
    scope: Scope = "project"
    settings: RepositorySettings = Field(default_factory=RepositorySettings)
    # The project role $git gets for merge automation; the Automation role when omitted.
    automation_role_id: int | None = Field(default=None, gt=0)

    _check_name = field_validator("repo_full_name")(_validate_repo_full_name)
    _check_url = field_validator("repo_url")(_validate_repo_url)


class RepositoryIntegrationUpdate(BaseModel):
    """Partial update.

    Changing ``provider`` moves the integration to another forge: its hook URL
    changes (the old one answers 404 at once) and a new secret is generated
    and returned once. Provider, URL and repository are checked together
    whenever one of them is sent.
    """

    provider: Provider | None = None
    repo_full_name: str | None = None
    repo_url: str | None = None
    scope: Scope | None = None
    settings: RepositorySettings | None = None
    is_active: bool | None = None
    # The project role $git gets for merge automation; its current role (or Automation) when omitted.
    automation_role_id: int | None = Field(default=None, gt=0)

    @field_validator("repo_full_name")
    @classmethod
    def validate_repo_full_name(cls, v: str | None) -> str | None:
        return None if v is None else _validate_repo_full_name(v)

    @field_validator("repo_url")
    @classmethod
    def validate_repo_url(cls, v: str | None) -> str | None:
        return None if v is None else _validate_repo_url(v)


class RepositoryIntegrationOut(BaseModel):
    """An integration. ``hook_url`` is where the forge sends its webhooks."""

    model_config = {"from_attributes": True}

    id: int
    public_id: uuid.UUID
    project_id: int
    provider: str
    repo_full_name: str
    repo_url: str
    default_branch: str
    scope: str
    settings: RepositorySettings
    is_active: bool
    last_delivery_at: datetime | None
    hook_url: str
    # The project role $git holds for merge automation, if it holds one.
    automation_role_id: int | None = None
    created_at: datetime
    updated_at: datetime


class RepositoryIntegrationSecretOut(RepositoryIntegrationOut):
    """An integration with its secret in clear: returned only by create and rotate-secret."""

    secret: str


class RepositoryIntegrationUpdateOut(RepositoryIntegrationOut):
    """An updated integration; ``secret`` is set, once, only when the provider changed."""

    secret: str | None = None


class RepositoryDeliveryOut(BaseModel):
    """One webhook delivery received for an integration, with the provider it came from."""

    model_config = {"from_attributes": True}

    id: int
    delivery_id: str
    provider: str
    event: str
    status: str
    detail: str | None
    received_at: datetime
    processed_at: datetime | None


class DevelopmentCommitOut(BaseModel):
    """A commit linked to an issue. The author email is never shown: it is not authenticated.

    ``id`` names the commit for detaching or moving it.
    """

    id: int | None = None
    sha: str
    short_sha: str
    first_line: str
    url: str
    branch: str | None
    author_name: str
    committed_at: datetime | None
    repository: str
    provider: str
    # The forge's page for ``branch``, for the issue page only: kept out of
    # the API and MCP output, which carry branch links in ``branches``.
    branch_url: SkipJsonSchema[str | None] = Field(default=None, exclude=True)


class DevelopmentPullRequestOut(BaseModel):
    """A pull/merge request linked to an issue; ``closing`` when it says it fixes the issue.

    ``id`` names the request for detaching or moving it.
    """

    id: int | None = None
    number: int
    title: str
    url: str
    state: str
    source_branch: str | None
    target_branch: str | None
    author_login: str | None
    merged_at: datetime | None
    closing: bool
    repository: str
    provider: str


class DevelopmentBranchOut(BaseModel):
    """A branch an issue was worked on: a linked commit's or pull request's, other than the default branch."""

    name: str
    url: str | None
    repository: str
    provider: str


class DevelopmentReviewOut(BaseModel):
    """One reviewer's latest verdict on a linked pull/merge request."""

    pull_request_id: int
    pull_request_number: int
    reviewer_login: str
    state: str
    submitted_at: datetime
    url: str
    repository: str
    provider: str


class DevelopmentCiRunOut(BaseModel):
    """A CI run, job or check of the issue's commits, pull requests or branches."""

    id: int
    run_type: str
    name: str
    status: str
    head_sha: str | None
    branch: str | None
    pull_request_number: int | None
    url: str
    attempt: int
    started_at: datetime | None
    completed_at: datetime | None
    occurred_at: datetime
    repository: str
    provider: str


class DevelopmentDeploymentOut(BaseModel):
    """A deployment of the issue's commits or branches; ``environment_type`` is None when unclassified."""

    id: int
    environment: str
    environment_type: str | None
    status: str
    sha: str | None
    ref: str | None
    url: str
    deployed_at: datetime | None
    occurred_at: datetime
    repository: str
    provider: str


class DevelopmentReleaseOut(BaseModel):
    """A tag or release the issue was released in, and the project version of the same name if there is one.

    ``source`` says why: ``tag_commit`` (the tag points at a linked commit),
    ``tag_push`` (the push that created the tag carried one) or
    ``default_branch`` (it reached the default branch before the tagged commit).
    """

    id: int
    tag_name: str
    name: str
    is_release: bool
    sha: str | None
    url: str
    released_at: datetime | None
    occurred_at: datetime
    source: str
    version_id: int | None
    version_name: str | None
    repository: str
    provider: str


class DevelopmentEnvironmentOut(BaseModel):
    """The latest successful deployment to one environment type (None: unclassified environments)."""

    environment_type: str | None
    environment: str
    deployed_at: datetime
    url: str
    repository: str
    provider: str


class DevelopmentRepositoryOut(BaseModel):
    """A repository the issue has activity in, with its link on the forge."""

    name: str
    url: str
    provider: str


class DevelopmentSummaryOut(BaseModel):
    """Counts and one combined status per kind.

    - ``pull_requests``: ``open`` if any is open (or draft), else ``merged``, else ``closed``;
    - ``reviews``: ``changes_requested`` if any reviewer asks for changes, else ``approved``, else ``commented``;
    - ``ci``: ``failing`` if the latest run of any check or workflow failed, else ``running``,
      else ``passing``, else ``neutral``;
    - each is None when there is nothing of that kind.
    """

    counts: dict[str, int]
    pull_requests: str | None = None
    reviews: str | None = None
    ci: str | None = None
    deployments: list[DevelopmentEnvironmentOut] = Field(default_factory=list)
    released_in: list[DevelopmentReleaseOut] = Field(default_factory=list)
    repositories: list[DevelopmentRepositoryOut] = Field(default_factory=list)


class DevelopmentTimelineItemOut(BaseModel):
    """One entry of an issue's development timeline.

    ``kind`` is ``commit``, ``pull_request``, ``review``, ``ci``,
    ``deployment`` or ``release``; ``id`` is the item's id within its kind.
    ``title`` is the commit's first line, the request's title, the run's
    name, the environment, or the tag; ``state`` the status of the kind.
    """

    kind: str
    id: int
    occurred_at: datetime
    title: str
    state: str | None = None
    url: str = ""
    repository: str
    provider: str
    sha: str | None = None
    branch: str | None = None
    number: int | None = None
    actor: str | None = None


DEVELOPMENT_KINDS = ("commit", "pull_request", "review", "ci", "deployment", "release")
DevelopmentKind = Literal["commit", "pull_request", "review", "ci", "deployment", "release"]


class IssueDevelopmentOut(BaseModel):
    """The development activity of an issue. ``commits`` holds the newest; ``commit_count`` all of them.

    ``summary`` gives counts and combined statuses; ``timeline`` everything in
    one list, newest first, filtered and paged by the request's ``kinds``,
    ``offset`` and ``limit``.
    """

    commit_count: int
    commits: list[DevelopmentCommitOut]
    pull_requests: list[DevelopmentPullRequestOut]
    branches: list[DevelopmentBranchOut]
    reviews: list[DevelopmentReviewOut] = Field(default_factory=list)
    ci_runs: list[DevelopmentCiRunOut] = Field(default_factory=list)
    deployments: list[DevelopmentDeploymentOut] = Field(default_factory=list)
    releases: list[DevelopmentReleaseOut] = Field(default_factory=list)
    summary: DevelopmentSummaryOut | None = None
    timeline: PaginatedResponse[DevelopmentTimelineItemOut] | None = None


class MoveDevelopmentItemIn(BaseModel):
    """Where to move a commit or pull request: another issue, by its key."""

    target_issue_ref: str = Field(min_length=3, max_length=50)
