"""Schemas for SSO provider configuration, group mappings and authentication settings.

Field-level rules live here as annotated types, so ``SsoProviderCreate`` and
``SsoProviderUpdate`` share them. Rules that need several fields or the
stored row (secret source vs. auth method, link policy vs. trust, http vs.
``SSO_ALLOW_INSECURE_HTTP``) are enforced by
:class:`specivo.services.sso.provider_service.ProviderService`.

The client secret is a ``SecretStr`` with no constraints of its own: a
field-level validation error echoes the field's input in the 422 envelope, so
the secret's own checks run in the service, whose errors never carry it.
For the same reason these models have no model-level validators (their
input would be the whole body, secret included).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

import httpx
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, SecretStr

from specivo.models.sso import CLIENT_SECRET_ENV_PREFIX, SLUG_PATTERN, is_allowed_client_secret_env

__all__ = [
    "EXTRA_AUTH_PARAM_KEYS",
    "SSO_SESSION_MAX_AGE_HOURS",
    "AuthSettingsOut",
    "AuthSettingsUpdate",
    "SsoClaimConfig",
    "SsoGroupMappingIn",
    "SsoGroupMappingOut",
    "SsoProviderCreate",
    "SsoProviderOut",
    "SsoProviderUpdate",
    "SsoSelfTestResult",
    "UserIdentityOut",
]

#: Authorization request parameters an admin may add per provider. Anything
#: else (``redirect_uri``, ``state``, ``nonce``, ``scope``, PKCE ...) is set by
#: Specivo and must never be overridable from configuration.
EXTRA_AUTH_PARAM_KEYS: frozenset[str] = frozenset({"prompt", "domain_hint", "login_hint", "acr_values", "max_age"})

TokenEndpointAuthMethod = Literal["client_secret_basic", "client_secret_post", "none"]
LinkPolicy = Literal["manual", "auto_verified_email"]
#: What an unreachable IdP means when a session is re-checked there.
IdpCheckFailure = Literal["allow", "deny"]
#: What a back-channel logout ends.
BackchannelAction = Literal["revoke_session", "revoke_all_sessions", "deprovision"]
SessionRecheckStatus = Literal["ok", "no_refresh_token", "refresh_refused"]
SecretStatusValue = Literal[
    "configured", "unreadable", "env", "env_missing", "env_not_allowed", "missing", "not_required"
]

_SLUG_RE = re.compile(SLUG_PATTERN)
# RFC 6749 section 3.3: scope-token = 1*( %x21 / %x23-5B / %x5D-7E )
_SCOPE_TOKEN_RE = re.compile(r"^[\x21\x23-\x5B\x5D-\x7E]+$")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")
_CLAIM_PATH_RE = re.compile(r"^\S{1,255}$")
_MAX_LIST_ITEMS = 200
_MAX_GROUP_LEN = 512

#: Upper bound of ``session_max_age_hours`` (one week). A session outlives a
#: change at the IdP (account disabled, group removed) until it reaches this
#: age, unless back-channel logout ends it sooner.
SSO_SESSION_MAX_AGE_HOURS = 168


# ---------------------------------------------------------------------------
# Field rules
# ---------------------------------------------------------------------------


def _check_slug(value: str) -> str:
    if not _SLUG_RE.fullmatch(value):
        raise ValueError("slug must be 2-40 characters: lowercase letters, digits and '-'")
    return value


def _check_url(value: str) -> str:
    value = value.strip()
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, TypeError, ValueError):
        raise ValueError("must be an absolute http(s) URL") from None
    if url.scheme not in ("https", "http") or not url.host:
        raise ValueError("must be an absolute http(s) URL")
    if url.fragment or "#" in value:
        raise ValueError("must not contain a fragment")
    if url.userinfo:
        raise ValueError("must not contain credentials")
    return value


def _check_env_name(value: str) -> str:
    if not is_allowed_client_secret_env(value):
        raise ValueError(
            f"must be an environment variable named {CLIENT_SECRET_ENV_PREFIX}<NAME> "
            "(A-Z, 0-9 and '_', at most 64 characters)"
        )
    return value


def _normalize_scopes(value: str) -> str:
    tokens: list[str] = []
    for token in value.split():
        if not _SCOPE_TOKEN_RE.fullmatch(token):
            raise ValueError(f"invalid scope {token!r}")
        if token not in tokens:
            tokens.append(token)
    if "openid" not in tokens:
        raise ValueError("scopes must include 'openid'")
    # openid first, the rest in the order given.
    tokens.remove("openid")
    return " ".join(["openid", *tokens])


def _check_group(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("group names must not be empty")
    if len(value) > _MAX_GROUP_LEN:
        raise ValueError(f"group names are at most {_MAX_GROUP_LEN} characters")
    return value


def _normalize_groups(values: list[str]) -> list[str]:
    result: list[str] = []
    for raw in values:
        value = _check_group(raw)
        if value not in result:
            result.append(value)
    return result


def _normalize_domains(values: list[str]) -> list[str]:
    result: list[str] = []
    for raw in values:
        value = raw.strip().lower().lstrip("@")
        if not _DOMAIN_RE.fullmatch(value):
            raise ValueError(f"invalid domain {raw!r}")
        if value not in result:
            result.append(value)
    return result


def _stringify(value: Any) -> Any:
    # max_age is naturally a number; store every parameter as a string.
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    return value


def _check_extra_params(values: dict[str, str]) -> dict[str, str]:
    unknown = sorted(set(values) - EXTRA_AUTH_PARAM_KEYS)
    if unknown:
        allowed = ", ".join(sorted(EXTRA_AUTH_PARAM_KEYS))
        raise ValueError(f"unsupported parameter(s) {', '.join(unknown)}; allowed: {allowed}")
    for key, value in values.items():
        if not value or len(value) > 512:
            raise ValueError(f"{key} must be 1-512 characters")
        if key == "max_age" and not value.isdigit():
            raise ValueError("max_age must be a non-negative integer")
    return values


Slug = Annotated[str, AfterValidator(_check_slug)]
HttpUrlStr = Annotated[str, Field(max_length=1024), AfterValidator(_check_url)]
IssuerStr = Annotated[str, Field(max_length=512), AfterValidator(_check_url)]
EnvName = Annotated[str, AfterValidator(_check_env_name)]
Scopes = Annotated[str, Field(max_length=512), AfterValidator(_normalize_scopes)]
GroupList = Annotated[list[str], Field(max_length=_MAX_LIST_ITEMS), AfterValidator(_normalize_groups)]
DomainList = Annotated[list[str], Field(max_length=_MAX_LIST_ITEMS), AfterValidator(_normalize_domains)]
ExtraParams = Annotated[
    dict[str, Annotated[str, BeforeValidator(_stringify)]],
    AfterValidator(_check_extra_params),
]
ClaimPath = Annotated[str, Field(pattern=_CLAIM_PATH_RE.pattern)]


class SsoClaimConfig(BaseModel):
    """Which ID token / userinfo claims feed the Specivo user. ``None`` uses the default."""

    model_config = ConfigDict(extra="forbid")

    username_claim: ClaimPath | None = None
    email_claim: ClaimPath | None = None
    name_claim: ClaimPath | None = None
    #: Dotted path, e.g. ``groups``, ``realm_access.roles``.
    groups_claim: ClaimPath | None = None
    locale_claim: ClaimPath | None = None
    #: Off by default. A claim the IdP guarantees to equal the SCIM ``externalId``
    #: it sends (Entra ID: ``oid`` = objectId); a match links a SCIM-provisioned
    #: account at the first sign-in without a trusted email.
    scim_external_id_claim: ClaimPath | None = None
    use_userinfo: bool = False


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class SsoProviderCreate(BaseModel):
    """Request body to create a provider. Providers are created disabled by default."""

    model_config = ConfigDict(extra="forbid")

    slug: Slug
    display_name: str = Field(min_length=1, max_length=255)
    enabled: bool = False
    position: int = Field(0, ge=0, le=10_000)

    issuer: IssuerStr
    discovery_url: HttpUrlStr | None = None
    client_id: str = Field(min_length=1, max_length=255)
    token_endpoint_auth_method: TokenEndpointAuthMethod = "client_secret_basic"
    #: Write-only. Stored encrypted; never returned.
    client_secret: SecretStr | None = None
    #: Alternative to ``client_secret``: the name of an environment variable holding it.
    #: Only ``SPECIVO_SSO_SECRET_*`` names are accepted, never Specivo's own settings.
    client_secret_env: EnvName | None = None
    scopes: Scopes = "openid profile email"
    claim_config: SsoClaimConfig = Field(default_factory=SsoClaimConfig)

    allow_jit_provisioning: bool = False
    link_policy: LinkPolicy = "manual"
    trust_email_verified: bool = False
    allowed_email_domains: DomainList = Field(default_factory=list)
    required_groups: GroupList = Field(default_factory=list)
    admin_groups: GroupList = Field(default_factory=list)
    sync_admin_flag: bool = False
    sync_groups: bool = False
    deactivate_on_access_loss: bool = False

    rp_logout_enabled: bool = True
    backchannel_logout_enabled: bool = False
    #: revoke_session: the IdP session's Specivo sessions; revoke_all_sessions:
    #: every session of the identity; deprovision: that, and every session and
    #: API key of an account that signs in only through SSO.
    backchannel_action: BackchannelAction = "revoke_session"
    #: Renew a session only after the IdP accepted its refresh token.
    verify_session_at_idp: bool = True
    #: What an unreachable IdP means for that check: allow renews, deny ends the session.
    idp_check_failure: IdpCheckFailure = "allow"
    session_max_age_hours: int = Field(24, ge=1, le=SSO_SESSION_MAX_AGE_HOURS)
    extra_auth_params: ExtraParams = Field(default_factory=dict)


class SsoProviderUpdate(BaseModel):
    """PATCH body: only the fields present are applied.

    ``client_secret`` replaces the stored secret (and drops an env reference),
    ``client_secret_env`` switches to an env reference (and drops the stored
    secret), ``clear_client_secret`` removes both. ``discovery_url: null``
    goes back to ``issuer + /.well-known/openid-configuration``.
    """

    model_config = ConfigDict(extra="forbid")

    slug: Slug | None = None
    display_name: str | None = Field(None, min_length=1, max_length=255)
    enabled: bool | None = None
    position: int | None = Field(None, ge=0, le=10_000)

    issuer: IssuerStr | None = None
    discovery_url: HttpUrlStr | None = None
    client_id: str | None = Field(None, min_length=1, max_length=255)
    token_endpoint_auth_method: TokenEndpointAuthMethod | None = None
    client_secret: SecretStr | None = None
    client_secret_env: EnvName | None = None
    clear_client_secret: bool = False
    scopes: Scopes | None = None
    claim_config: SsoClaimConfig | None = None

    allow_jit_provisioning: bool | None = None
    link_policy: LinkPolicy | None = None
    trust_email_verified: bool | None = None
    allowed_email_domains: DomainList | None = None
    required_groups: GroupList | None = None
    admin_groups: GroupList | None = None
    sync_admin_flag: bool | None = None
    sync_groups: bool | None = None
    deactivate_on_access_loss: bool | None = None

    rp_logout_enabled: bool | None = None
    backchannel_logout_enabled: bool | None = None
    backchannel_action: BackchannelAction | None = None
    verify_session_at_idp: bool | None = None
    idp_check_failure: IdpCheckFailure | None = None
    session_max_age_hours: int | None = Field(None, ge=1, le=SSO_SESSION_MAX_AGE_HOURS)
    extra_auth_params: ExtraParams | None = None


class SsoProviderOut(BaseModel):
    """A provider as the admin API shows it. The client secret is never included."""

    id: int
    slug: str
    display_name: str
    enabled: bool
    position: int

    issuer: str
    discovery_url: str | None
    client_id: str
    token_endpoint_auth_method: str
    #: True when a secret source is set (stored secret or env var name).
    client_secret_configured: bool
    #: ``configured`` | ``unreadable`` (re-enter it) | ``env`` | ``env_missing``
    #: (the env var is unset) | ``env_not_allowed`` (name outside ``SPECIVO_SSO_SECRET_*``,
    #: never read) | ``missing`` | ``not_required`` (public client).
    client_secret_status: SecretStatusValue
    client_secret_env: str | None
    scopes: str
    claim_config: dict[str, Any]

    allow_jit_provisioning: bool
    link_policy: str
    trust_email_verified: bool
    allowed_email_domains: list[str]
    required_groups: list[str]
    admin_groups: list[str]
    sync_admin_flag: bool
    sync_groups: bool
    deactivate_on_access_loss: bool

    rp_logout_enabled: bool
    backchannel_logout_enabled: bool
    backchannel_action: str
    verify_session_at_idp: bool
    idp_check_failure: str
    #: What the last sign-in found: ``ok`` (sessions are re-checked),
    #: ``no_refresh_token`` or ``refresh_refused`` (they are not); ``null``
    #: before the first sign-in with the check on.
    session_recheck_status: SessionRecheckStatus | None = None
    session_recheck_status_at: datetime | None = None
    session_max_age_hours: int
    extra_auth_params: dict[str, str]

    discovery_fetched_at: datetime | None
    identity_count: int
    #: User-group memberships this provider manages; deleting it removes them.
    managed_membership_count: int = 0
    #: Register these at the identity provider, byte for byte.
    redirect_uri: str
    backchannel_logout_uri: str

    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Group mappings
# ---------------------------------------------------------------------------


class SsoGroupMappingIn(BaseModel):
    """One IdP group value (exact, case-sensitive) mapped to a Specivo user group."""

    model_config = ConfigDict(extra="forbid")

    idp_group: Annotated[str, AfterValidator(_check_group)]
    user_group_id: int = Field(ge=1)


class SsoGroupMappingOut(BaseModel):
    id: int
    idp_group: str
    user_group_id: int
    user_group_name: str


class UserIdentityOut(BaseModel):
    """A user's connected SSO identity. The external subject is not exposed."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    provider_id: int
    provider_slug: str
    provider_name: str
    provider_enabled: bool
    linked_via: str
    email_at_last_login: str | None
    last_login_at: datetime | None
    linked_at: datetime | None


# ---------------------------------------------------------------------------
# Global authentication settings
# ---------------------------------------------------------------------------


class AuthSettingsOut(BaseModel):
    """Instance-wide sign-in settings (``settings`` table, ``sso.*`` keys)."""

    #: Hide the username/password form on the login page (break-glass: ``?local=1``).
    hide_local_login: bool
    #: Only administrators may sign in with a local password.
    local_login_admins_only: bool
    #: Slug of a provider the login page redirects to automatically.
    auto_redirect_provider: str | None
    #: Whether users who sign in through SSO may create and use API keys.
    allow_api_keys_for_sso_users: bool


class AuthSettingsUpdate(BaseModel):
    """PATCH body: only the fields present are applied; ``auto_redirect_provider: null`` clears it."""

    model_config = ConfigDict(extra="forbid")

    hide_local_login: bool | None = None
    local_login_admins_only: bool | None = None
    auto_redirect_provider: Slug | None = None
    allow_api_keys_for_sso_users: bool | None = None


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


class SsoSelfTestResult(BaseModel):
    """What the discovery self-test found. Nothing is stored, whatever the outcome.

    ``error_code`` is ``sso_idp_unreachable`` or ``sso_discovery_invalid``;
    ``error_reason`` is the machine reason (``issuer_mismatch``, ``timeout``,
    ``http_404``, ``jwks_no_usable_keys`` ...). ``warnings`` are codes for
    configuration that works but is probably not what the admin wants.
    """

    ok: bool
    failed_step: Literal["discovery", "jwks"] | None = None
    error_code: str | None = None
    error_reason: str | None = None

    discovery_url: str
    expected_issuer: str
    reported_issuer: str | None = None
    issuer_match: bool | None = None

    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    userinfo_endpoint: str | None = None
    end_session_endpoint: str | None = None
    jwks_uri: str | None = None

    signing_algorithms: list[str] = Field(default_factory=list)
    usable_signing_algorithms: list[str] = Field(default_factory=list)
    token_endpoint_auth_methods: list[str] = Field(default_factory=list)
    pkce_s256: bool | None = None
    iss_parameter_supported: bool | None = None
    backchannel_logout_supported: bool | None = None
    backchannel_logout_session_supported: bool | None = None
    #: ``refresh_token`` in the discovery ``grant_types_supported``; ``null`` when the list is absent.
    refresh_grant_advertised: bool | None = None

    #: The provider's ``verify_session_at_idp`` and what the last sign-in
    #: found (``SsoProviderOut.session_recheck_status``). Not tested live: only
    #: a sign-in yields a refresh token.
    session_recheck_enabled: bool = False
    session_recheck_status: SessionRecheckStatus | None = None
    session_recheck_status_at: datetime | None = None

    jwks_key_count: int | None = None
    jwks_key_ids: list[str] = Field(default_factory=list)

    warnings: list[str] = Field(default_factory=list)


class ScimSettingsOut(BaseModel):
    """A provider's SCIM provisioning settings. The token itself is never included."""

    enabled: bool
    #: Paste into the IdP as the SCIM base (tenant) URL.
    base_url: str
    token_configured: bool
    #: The first characters of the token, to tell tokens apart.
    token_prefix: str | None
    token_created_at: datetime | None
    #: The last authenticated SCIM request.
    last_request_at: datetime | None
    user_count: int
    group_count: int
    #: SCIM is on and ``sync_groups`` too: the groups claim is ignored at sign-in.
    claim_group_sync_skipped: bool


class ScimSettingsUpdate(BaseModel):
    """Switch SCIM provisioning on or off; on needs a token."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class ScimTokenOut(ScimSettingsOut):
    """The settings plus the new token, returned once by the generate call."""

    token: str
