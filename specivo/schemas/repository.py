"""Pydantic schemas for repository integration endpoints."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    """Commit command switches. Stored now, acted on only once commit commands exist; all off by default."""

    model_config = ConfigDict(extra="forbid", strict=True)

    commands_enabled: bool = False
    time_logging_enabled: bool = False
    fixing_status_id: int | None = Field(default=None, gt=0)
    merge_status_id: int | None = Field(default=None, gt=0)
    merge_automation_enabled: bool = False


class RepositoryIntegrationCreate(BaseModel):
    """A new integration. The secret is always generated and returned once."""

    provider: Provider
    repo_full_name: str
    repo_url: str
    scope: Scope = "project"
    settings: RepositorySettings = Field(default_factory=RepositorySettings)

    _check_name = field_validator("repo_full_name")(_validate_repo_full_name)
    _check_url = field_validator("repo_url")(_validate_repo_url)


class RepositoryIntegrationUpdate(BaseModel):
    """Partial update. The provider cannot change: connect the repository again instead."""

    repo_full_name: str | None = None
    repo_url: str | None = None
    scope: Scope | None = None
    settings: RepositorySettings | None = None
    is_active: bool | None = None

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
