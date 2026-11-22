"""Pydantic schemas for the role editor (``/api/v1/admin/roles/``).

A role is a named set of permissions plus four visibility settings, assigned
to project memberships. Full access is ``permissions: ["*"]``. The permission
catalogue (groups, labels, dependencies) is described by
``PermissionCatalogueOut`` and produced by
``specivo.services.permission_catalogue.catalogue_payload``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

UsersVisibility = Literal["all", "members_of_visible_projects"]
EntriesVisibility = Literal["all", "own"]

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


def _clean_name(value: str) -> str:
    """Strip surrounding whitespace and reject an empty result."""
    value = value.strip()
    if not value:
        raise ValueError("name must not be empty")
    return value


class RoleCreate(BaseModel):
    """Body for ``POST /admin/roles/``.

    Without ``copy_from_role_id`` the role starts blank: ``permissions`` (or
    none) and the default visibility settings. With it, the new role copies
    that role's permissions, visibility settings and workflow rows, so
    ``permissions`` may not be given as well.
    """

    name: str = Field(max_length=255, description="Role name. Unique case-insensitively.")
    copy_from_role_id: int | None = Field(default=None, gt=0, description="Copy this role instead of starting blank.")
    permissions: list[str] | None = Field(default=None, description='Permission keys, or ["*"] for Full access.')
    assignable: bool = Field(default=True, description="Offered when adding members to a project.")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return _clean_name(v)

    @model_validator(mode="after")
    def permissions_or_copy(self) -> Self:
        if self.copy_from_role_id is not None and self.permissions is not None:
            raise ValueError("permissions cannot be given together with copy_from_role_id")
        return self


class RoleUpdate(BaseModel):
    """Body for ``PATCH /admin/roles/{role_id}/``.

    ``lock_version`` is the version the client last read; a role changed
    since returns 409 ``conflict_lock_version``. Only the fields present in
    the body are applied, and none of them may be ``null``.
    ``issues_visibility`` is read-only here.
    """

    lock_version: int = Field(ge=0)
    name: str | None = Field(default=None, max_length=255)
    assignable: bool | None = None
    permissions: list[str] | None = Field(default=None, description='Permission keys, or ["*"] for Full access.')
    users_visibility: UsersVisibility | None = None
    time_entries_visibility: EntriesVisibility | None = None
    ai_usage_visibility: EntriesVisibility | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        return None if v is None else _clean_name(v)

    @model_validator(mode="after")
    def no_explicit_null(self) -> Self:
        nulls = sorted(name for name in self.model_fields_set if getattr(self, name) is None)
        if nulls:
            raise ValueError(f"{', '.join(nulls)} cannot be null")
        return self


class RoleMove(BaseModel):
    """Body for ``POST /admin/roles/{role_id}/move/``."""

    direction: Literal["up", "down"]


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class RoleOut(BaseModel):
    """A role as the role list and editor show it.

    ``full_access`` is True when ``permissions`` is ``["*"]``.
    ``membership_count`` counts the project memberships (user and group,
    inherited ones included) holding the role.
    """

    model_config = {"from_attributes": True}

    id: int
    name: str
    position: int
    assignable: bool
    builtin: int
    system_key: str | None
    full_access: bool
    permissions: list[str]
    issues_visibility: str
    users_visibility: str
    time_entries_visibility: str
    ai_usage_visibility: str
    lock_version: int
    membership_count: int = 0
    created_at: datetime
    updated_at: datetime


class RoleProjectOut(BaseModel):
    """A project where a membership holds the role."""

    id: int
    key: str
    name: str


class RoleDetailOut(RoleOut):
    """A role with what depends on it: the projects using it and its workflow rows."""

    projects: list[RoleProjectOut] = []
    workflow_transition_count: int = 0
    field_rule_count: int = 0


class PermissionInfoOut(BaseModel):
    key: str
    label: str
    description: str
    enforced: bool
    requires: list[str] = Field(description="Every prerequisite, direct or not, in catalogue order.")


class PermissionGroupOut(BaseModel):
    key: str
    label: str
    module: str | None
    permissions: list[PermissionInfoOut]


class PermissionCatalogueOut(BaseModel):
    """Every permission, grouped as the role editor shows them, in the request's language."""

    groups: list[PermissionGroupOut]
    non_member_allowed: list[str]
    total: int
