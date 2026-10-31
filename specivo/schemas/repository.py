"""Pydantic schemas for repository integration endpoints."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    commands_enabled: bool = False
    time_logging_enabled: bool = False
    fixing_status_id: int | None = Field(default=None, gt=0)
    merge_status_id: int | None = Field(default=None, gt=0)
    merge_automation_enabled: bool = False
    ignore_external_pull_requests: bool = True

    @model_validator(mode="after")
    def _check_dependencies(self) -> RepositorySettings:
        if self.time_logging_enabled and not self.commands_enabled:
            raise ValueError("time_logging_enabled needs commands_enabled")
        if self.merge_automation_enabled and self.merge_status_id is None:
            raise ValueError("merge_automation_enabled needs merge_status_id")
        return self


class RepositoryIntegrationCreate(BaseModel):
    """A new integration. The secret is always generated and returned once."""

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
    """Partial update. The provider cannot change: connect the repository again instead."""

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


class RepositoryDeliveryOut(BaseModel):
    """One webhook delivery received for an integration."""

    model_config = {"from_attributes": True}

    id: int
    delivery_id: str
    event: str
    status: str
    detail: str | None
    received_at: datetime
    processed_at: datetime | None


class DevelopmentCommitOut(BaseModel):
    """A commit linked to an issue. The author email is never shown: it is not authenticated."""

    sha: str
    short_sha: str
    first_line: str
    url: str
    branch: str | None
    author_name: str
    committed_at: datetime | None
    repository: str
    provider: str


class DevelopmentPullRequestOut(BaseModel):
    """A pull/merge request linked to an issue; ``closing`` when it says it fixes the issue."""

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


class IssueDevelopmentOut(BaseModel):
    """The development activity of an issue. ``commits`` holds the newest; ``commit_count`` all of them."""

    commit_count: int
    commits: list[DevelopmentCommitOut]
    pull_requests: list[DevelopmentPullRequestOut]
    branches: list[DevelopmentBranchOut]
