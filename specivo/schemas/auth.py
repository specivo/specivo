"""Authentication-related Pydantic schemas (login, tokens, sessions, API keys)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    """Credentials for the login endpoint.

    The ``login`` field accepts either a username or an email address.
    """

    login: str
    password: str = Field(max_length=1024)


class TokenResponse(BaseModel):
    """JWT tokens returned on successful login or token refresh."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until access token expires


class RefreshRequest(BaseModel):
    """Body for POST /auth/refresh (cookie fallback handled in the router)."""

    refresh_token: str


class SessionOut(BaseModel):
    """A single active session (refresh token) returned by GET /auth/sessions."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    device_info: str | None
    ip_address: str | None
    created_at: datetime
    expires_at: datetime


# ---------------------------------------------------------------------------
# API Key schemas
# ---------------------------------------------------------------------------


class ApiKeyCreate(BaseModel):
    """Request body for creating an API key."""

    name: str = Field(min_length=1, max_length=255)
    scopes: dict | None = None
    expires_at: datetime | None = None
    ip_allowlist: list[str] | None = None


class ApiKeyCreated(BaseModel):
    """Response on API key creation — includes raw_key shown ONCE."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    key_prefix: str
    raw_key: str  # Only returned at creation time
    scopes: dict | None
    expires_at: datetime | None
    created_at: datetime


class ApiKeyOut(BaseModel):
    """API key representation for listing — no raw key, no hash."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    key_prefix: str
    scopes: dict | None
    expires_at: datetime | None
    last_used_at: datetime | None
    is_active: bool
    created_at: datetime


class ApiKeyPatch(BaseModel):
    """Request body for PATCH /my/api-keys/{id} — toggle active state."""

    is_active: bool
