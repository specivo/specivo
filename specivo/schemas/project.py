"""Pydantic schemas for Project, Member, and Module endpoints."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, field_validator, model_validator

# ---------------------------------------------------------------------------
# Project schemas
# ---------------------------------------------------------------------------

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9-]{0,98}[a-z0-9]$|^[a-z]$")
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,11}$")


_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class ProjectCreate(BaseModel):
    name: str
    identifier: str
    key: str
    description: str | None = None
    parent_key: str | None = None
    is_public: bool = False
    color: str | None = None
    modules: list[str] | None = None

    @field_validator("color")
    @classmethod
    def validate_color(cls, v: str | None) -> str | None:
        if v is not None and not _HEX_COLOR_RE.match(v):
            raise ValueError("color must be a valid hex color (e.g. #c49a3c)")
        return v

    @field_validator("identifier")
    @classmethod
    def validate_identifier(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[a-z][a-z0-9-]*[a-z0-9]$|^[a-z]$", v):
            raise ValueError(
                "identifier must be lowercase, start with a letter, and contain only letters, digits, and hyphens"
            )
        if len(v) > 100:
            raise ValueError("identifier must be 100 characters or fewer")
        return v

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        v = v.strip().upper()
        if not _KEY_RE.match(v):
            raise ValueError(
                "key must be 2–12 uppercase characters, start with a letter, and contain only letters and digits"
            )
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be empty")
        if len(v) > 255:
            raise ValueError("name must be 255 characters or fewer")
        return v


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_public: bool | None = None
    status: int | None = None
    color: str | None = None
    # parent_id: use model_fields_set to detect whether this field was provided.
    # - Not in payload          → not in model_fields_set  → do not change parent
    # - parent_id=null in JSON  → in model_fields_set, value is None  → move to root
    # - parent_id=<int>         → in model_fields_set, value is int   → reparent
    parent_id: int | None = None

    @field_validator("color")
    @classmethod
    def validate_color(cls, v: str | None) -> str | None:
        if v is not None and not _HEX_COLOR_RE.match(v):
            raise ValueError("color must be a valid hex color (e.g. #c49a3c)")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("name must not be empty")
            if len(v) > 255:
                raise ValueError("name must be 255 characters or fewer")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: int | None) -> int | None:
        if v is not None and v not in (1, 5, 9):
            raise ValueError("status must be 1 (active), 5 (closed), or 9 (archived)")
        return v


class ProjectOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    name: str
    identifier: str
    key: str
    description: str | None
    parent_id: int | None
    parent_key: str | None = None
    path: str
    is_public: bool
    inherit_members: bool
    status: int
    issue_sequence: int
    color: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Rename schemas (admin-only)
# ---------------------------------------------------------------------------


class ProjectRenameRequest(BaseModel):
    new_key: str | None = None
    new_identifier: str | None = None
    reason: str | None = None

    @field_validator("new_key")
    @classmethod
    def validate_new_key(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip().upper()
            if not _KEY_RE.match(v):
                raise ValueError("new_key must be 2-12 uppercase chars, start with a letter")
        return v

    @field_validator("new_identifier")
    @classmethod
    def validate_new_identifier(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip().lower()
            if not _IDENTIFIER_RE.match(v):
                raise ValueError("new_identifier must be lowercase, start with a letter, letters/digits/hyphens only")
            if len(v) > 100:
                raise ValueError("new_identifier must be 100 characters or fewer")
        return v

    @model_validator(mode="after")
    def at_least_one_field(self) -> ProjectRenameRequest:
        if self.new_key is None and self.new_identifier is None:
            raise ValueError("At least one of new_key or new_identifier must be provided")
        return self


class ProjectRenameOut(ProjectOut):
    old_key: str | None = None
    old_identifier: str | None = None
    issues_rekeyed: int = 0


# ---------------------------------------------------------------------------
# Member schemas
# ---------------------------------------------------------------------------


class MemberAdd(BaseModel):
    user_id: int
    role_ids: list[int]

    @model_validator(mode="after")
    def check_role_ids(self) -> MemberAdd:
        if not self.role_ids:
            raise ValueError("role_ids must contain at least one role")
        return self


class MemberUpdateRoles(BaseModel):
    role_ids: list[int]

    @model_validator(mode="after")
    def check_role_ids(self) -> MemberUpdateRoles:
        if not self.role_ids:
            raise ValueError("role_ids must contain at least one role")
        return self


class MemberOut(BaseModel):
    model_config = {"from_attributes": True}

    user_id: int
    login: str
    display_name: str
    roles: list[str]
    role_ids: list[int] = []


# ---------------------------------------------------------------------------
# Module schemas
# ---------------------------------------------------------------------------

KNOWN_MODULES = frozenset({"issue_tracking", "wiki", "time_tracking"})


class ModuleToggle(BaseModel):
    modules: dict[str, bool]

    @field_validator("modules")
    @classmethod
    def validate_modules(cls, v: dict[str, bool]) -> dict[str, bool]:
        unknown = set(v.keys()) - KNOWN_MODULES
        if unknown:
            raise ValueError(f"Unknown modules: {', '.join(sorted(unknown))}")
        return v


class ModulesOut(BaseModel):
    modules: dict[str, bool]
