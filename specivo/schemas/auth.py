"""Authentication-related Pydantic schemas (login, tokens, sessions, API keys)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, field_validator
from pydantic import ValidationError as PydanticValidationError
from pydantic_core import PydanticCustomError

from specivo.core.i18n import gettext as _


class LoginRequest(BaseModel):
    """Credentials for the login endpoint.

    The ``login`` field accepts either a username or an email address.
    """

    login: str
    password: str = Field(max_length=1024)
    remember: bool = False


class TokenResponse(BaseModel):
    """JWT tokens returned on successful login or token refresh."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until access token expires


class RefreshRequest(BaseModel):
    """Body for POST /auth/refresh (cookie fallback handled in the router)."""

    refresh_token: str


class LogoutResponse(BaseModel):
    """Result of POST /auth/logout: where to end the session at the identity provider."""

    end_session_url: str | None = Field(
        default=None,
        description=(
            "For a single sign-on session whose provider does RP-initiated logout: the identity "
            "provider's end-session URL to send the user to. null for a local session, or when "
            "the provider cannot be reached (the Specivo session is ended either way)."
        ),
    )


class SessionOut(BaseModel):
    """A single active session (refresh token) returned by GET /auth/sessions."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    device_info: str | None
    ip_address: str | None
    created_at: datetime
    expires_at: datetime
    # "password" or "sso"
    auth_method: str
    # Display name of the SSO provider the session signed in with; None for
    # password sessions (and SSO sessions whose identity was unlinked).
    provider_name: str | None = None


# ---------------------------------------------------------------------------
# API Key schemas
# ---------------------------------------------------------------------------


#: Most projects one key may be limited to.
API_KEY_SCOPE_MAX_PROJECTS = 200

_ProjectRef = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=100)]


class ApiKeyScopes(BaseModel):
    """The project limit of an API key: ``{"projects": [...]}``, projects named by key or id.

    An empty list is refused rather than read as "unlimited", and any other
    key is refused rather than stored and ignored. ``create_key`` resolves the
    entries to project ids and stores ``{"projects": [ids]}``.
    """

    model_config = ConfigDict(extra="forbid")

    projects: list[_ProjectRef | StrictInt] = Field(min_length=1, max_length=API_KEY_SCOPE_MAX_PROJECTS)


def api_key_scopes_message() -> str:
    """The refusal for a ``scopes`` value that is not a valid :class:`ApiKeyScopes`."""
    return _('The project limit must be an object with a single "projects" list naming 1 to 200 projects by key or id.')


def parse_api_key_scopes(value: Any) -> ApiKeyScopes | None:
    """Return *value* as :class:`ApiKeyScopes` (None for no limit); raise ``ValueError`` when it is malformed."""
    if value is None or isinstance(value, ApiKeyScopes):
        return value
    if not isinstance(value, dict):
        raise ValueError(api_key_scopes_message())
    try:
        return ApiKeyScopes.model_validate(value)
    except PydanticValidationError:
        raise ValueError(api_key_scopes_message()) from None


class ApiKeyCreate(BaseModel):
    """Request body for creating an API key."""

    name: str = Field(min_length=1, max_length=255)
    scopes: ApiKeyScopes | None = None
    expires_at: datetime | None = None
    ip_allowlist: list[str] | None = None

    @field_validator("scopes", mode="before")
    @classmethod
    def _check_scopes(cls, value: Any) -> ApiKeyScopes | None:
        # One translatable refusal on ``scopes`` itself, whatever is wrong inside.
        try:
            return parse_api_key_scopes(value)
        except ValueError as exc:
            raise PydanticCustomError("api_key_scopes", str(exc).replace("{", "{{").replace("}", "}}")) from None


class ApiKeyCreated(BaseModel):
    """Response on API key creation — includes raw_key shown ONCE."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    key_prefix: str
    raw_key: str  # Only returned at creation time
    scopes: dict | None
    #: Keys of the projects the key is limited to (None: not limited).
    project_keys: list[str] | None = None
    expires_at: datetime | None
    created_at: datetime


class ApiKeyOut(BaseModel):
    """API key representation for listing — no raw key, no hash."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    key_prefix: str
    # A list is tolerated: a plugin may once have stored one. It does not limit the key.
    scopes: dict | list | None
    #: Keys of the projects the key is limited to, sorted (None: not limited). A project
    #: that no longer exists is left out, so an empty list means the key reaches nothing.
    project_keys: list[str] | None = None
    expires_at: datetime | None
    last_used_at: datetime | None
    is_active: bool
    created_at: datetime


class ApiKeyPatch(BaseModel):
    """Request body for PATCH /my/api-keys/{id} — toggle active state."""

    is_active: bool


# ---------------------------------------------------------------------------
# Password reset schemas
# ---------------------------------------------------------------------------


class ForgotPasswordRequest(BaseModel):
    """Request body for POST /auth/forgot-password."""

    email: str = Field(max_length=255)


class ResetPasswordRequest(BaseModel):
    """Request body for POST /auth/reset-password."""

    token: str
    new_password: str = Field(max_length=1024)


class ChangePasswordRequest(BaseModel):
    """Request body for POST /auth/change-password/ (authenticated user)."""

    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


class ChangePasswordResponse(BaseModel):
    """Result of a successful password change."""

    detail: str
    revoked_sessions: int
