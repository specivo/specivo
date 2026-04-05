"""User-related Pydantic schemas.

Three schema families:
- UserCreate: admin creating a new user (includes password).
- UserOut: public user response (no password_hash, no sensitive internals).
- UserProfile: current user's own profile (includes email, preferences).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class UserCreate(BaseModel):
    """Schema for admin-created users (POST /api/v1/admin/users).

    Password is optional — service accounts have no password.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    login: str = Field(
        min_length=3,
        max_length=100,
        pattern=r"^[a-z0-9_-]+$",
        description="Username: lowercase alphanumeric, underscore, hyphen only.",
    )
    email: EmailStr = Field(description="User email address.")
    password: str | None = Field(
        default=None,
        max_length=128,
        description="Plain-text password. Omit for service accounts.",
    )
    display_name: str = Field(
        min_length=1,
        max_length=255,
        description="Full name or display name.",
    )
    # Optional profile fields
    firstname: str | None = Field(default=None, max_length=255)
    lastname: str | None = Field(default=None, max_length=255)
    language: str = Field(default="en", min_length=2, max_length=10)
    timezone: str = Field(default="UTC", max_length=50)
    # Admin flags
    is_admin: bool = False
    is_service_account: bool = False
    status: Literal["active", "pending_verification"] = "active"


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class UserOut(BaseModel):
    """Public user response — safe to expose to any authenticated user.

    Excludes: password_hash, failed_login_count, locked_until, preferences,
    github_id, google_id, email (private).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    login: str
    display_name: str
    avatar_url: str | None
    is_service_account: bool
    created_at: datetime

    # Admin-visible fields included here for simplicity.
    # Endpoints should use UserProfile for /my/profile and
    # UserOut for /users/{id} (public view).
    is_admin: bool
    status: str
    language: str
    timezone: str
    last_login_at: datetime | None
    updated_at: datetime


class UserProfile(BaseModel):
    """Current user's own profile (GET /api/v1/my/profile).

    Includes private fields (email, preferences, verification timestamps)
    that should not be exposed in public user listings.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    login: str
    email: str
    display_name: str
    avatar_url: str | None
    language: str
    timezone: str
    status: str
    is_admin: bool
    is_service_account: bool
    email_verified_at: datetime | None
    last_login_at: datetime | None
    password_changed_at: datetime | None
    preferences: dict
    created_at: datetime
    updated_at: datetime
