"""Pydantic schemas for Project, Member, and Module endpoints."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, field_validator, model_validator

# ---------------------------------------------------------------------------
# Project schemas
# ---------------------------------------------------------------------------

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9-]{0,98}[a-z0-9]$|^[a-z]$")
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")


class ProjectCreate(BaseModel):
    name: str
    identifier: str
    key: str
    description: str | None = None
    parent_key: str | None = None
    is_public: bool = True

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
                "key must be 2–10 uppercase characters, start with a letter, and contain only letters and digits"
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
    created_at: datetime
    updated_at: datetime


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


class MemberOut(BaseModel):
    model_config = {"from_attributes": True}

    user_id: int
    login: str
    display_name: str
    roles: list[str]


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
