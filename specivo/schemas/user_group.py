"""Pydantic schemas for user groups — the membership principal.

A user group is a named set of users that can hold project memberships in its
own right.  These schemas back the admin API at ``/api/v1/admin/groups/``.

Not to be confused with ``schemas.agent_group``, which describes ``AgentGroup``
— an AI-agent access-policy construct with no bearing on project membership.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


def _clean_name(value: str) -> str:
    """Strip surrounding whitespace and reject an empty result."""
    value = value.strip()
    if not value:
        raise ValueError("name must not be empty")
    return value


class UserGroupCreate(BaseModel):
    """Body for ``POST /admin/groups/``."""

    name: str = Field(max_length=255, description="Group name. Unique case-insensitively.")
    description: str | None = Field(default=None, description="Optional free-text description.")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return _clean_name(v)


class UserGroupUpdate(BaseModel):
    """Body for ``PATCH /admin/groups/{group_id}/``.

    Both fields are optional; only the ones present in the request body are
    applied.  ``description: null`` clears the description, which is why the
    endpoint inspects ``model_fields_set`` rather than testing for ``None``.
    """

    name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _clean_name(v)


class UserGroupUserAdd(BaseModel):
    """Body for ``POST /admin/groups/{group_id}/users/``."""

    user_id: int


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class UserGroupOut(BaseModel):
    """A group on its own, without any of its links."""

    model_config = {"from_attributes": True}

    id: int
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class UserGroupListItem(UserGroupOut):
    """A row of the group list, carrying the size of what the group holds."""

    model_config = {"from_attributes": True}

    user_count: int = 0
    project_count: int = 0


class UserGroupProjectOut(BaseModel):
    """A project the group holds a membership on, and the roles it grants there."""

    model_config = {"from_attributes": True}

    project_id: int
    key: str
    name: str
    roles: list[str] = []


class UserGroupDetailOut(UserGroupOut):
    """Group detail: what the group is, and what access it currently grants.

    ``projects`` is the point of the endpoint — an admin about to delete a
    group needs to see the access that disappears with it.
    """

    model_config = {"from_attributes": True}

    user_count: int = 0
    projects: list[UserGroupProjectOut] = []


class UserGroupUserOut(BaseModel):
    """A user inside a group."""

    model_config = {"from_attributes": True}

    user_id: int
    login: str
    display_name: str
    avatar_url: str | None = None
