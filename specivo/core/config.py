"""Application configuration via pydantic-settings."""

from datetime import UTC, datetime
from functools import lru_cache
from importlib.metadata import version as _pkg_version

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_available_languages() -> list[str]:
    """Default factory for ``available_languages``.

    Returns the language codes with a compiled core catalog (en, es, fr,
    ru, zh). Imported lazily inside the function to avoid an import cycle
    between ``core.config`` and ``core.locales``.
    """
    from specivo.core.locales import get_available_locales

    return get_available_locales()


_ALLOWED_FTS_LANGUAGES: frozenset[str] = frozenset(
    {
        "simple",
        "arabic",
        "armenian",
        "basque",
        "catalan",
        "danish",
        "dutch",
        "english",
        "finnish",
        "french",
        "german",
        "greek",
        "hindi",
        "hungarian",
        "indonesian",
        "irish",
        "italian",
        "lithuanian",
        "nepali",
        "norwegian",
        "portuguese",
        "romanian",
        "russian",
        "serbian",
        "spanish",
        "swedish",
        "tamil",
        "turkish",
        "yiddish",
    }
)


# Upper bound for ``sso_clock_skew_seconds``: more leeway than this would let
# tokens outlive their expiry by a meaningful margin.
SSO_MAX_CLOCK_SKEW_SECONDS = 300


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),  # .env.local overrides .env (secrets)
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database — no defaults, MUST be set via .env or .env.local
    database_url: str

    # Redis — no default, MUST be set via .env or .env.local
    redis_url: str
    # Celery broker — defaults to redis_url. Override to isolate Celery from cache.
    celery_broker_url: str = ""

    # Auth — no default for secret_key, MUST be set
    secret_key: str
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30
    bcrypt_rounds: int = 12
    password_reset_token_expire_hours: int = 24
    app_url: str = "http://localhost:8030"  # Public base URL for links in emails

    # Registration
    registration_mode: str = "open"  # open, invite_only, disabled
    captcha_enabled: bool = False

    # Password policy
    password_min_length: int = 8  # minimum 6, enforced by validator

    # Legacy password hashes — accounts imported from another system keep their
    # password until their first sign-in, which replaces the hash with a native
    # bcrypt one. See ``specivo.services.password_hashers``.
    # Dotted paths to extra hasher classes, for a hasher shipped as one module
    # rather than as a plugin (``INSTALLED_PLUGINS``).
    password_hashers: list[str] = []
    # Allowlist of legacy algorithm ids that may verify a sign-in, and that the
    # importer may store. Empty: no legacy hash ever verifies.
    legacy_password_algorithms: list[str] = []
    # After this moment no legacy hash verifies, and the owners of the accounts
    # still holding one have to use password recovery. A naive value is UTC.
    legacy_password_hashes_expire_at: datetime | None = None

    # App
    debug: bool = False
    app_name: str = "Specivo"
    version: str = _pkg_version("specivo")
    api_v1_prefix: str = "/api/v1"

    # Stealth mode — secret URL prefix for all routes.
    # When set, all endpoints (API, docs, health, hooks) are mounted behind
    # this prefix, making the instance invisible to scanners probing known URLs.
    # Example: STEALTH_PREFIX=/s3cr3t → /s3cr3t/api/v1/projects
    stealth_prefix: str = ""

    # CORS — comma-separated origins, or "*" for development
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]
    cors_allow_credentials: bool = True

    # Trusted hosts — comma-separated, used by TrustedHostMiddleware in production
    allowed_hosts: list[str] = ["*"]

    # robots.txt — default content. Overridable from admin settings (key: "robots_txt").
    robots_txt: str = "User-agent: *\nDisallow: /\n"

    # SMTP — email notification delivery
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@specivo.dev"
    smtp_tls: bool = True

    # Kill switch — emergency token for mobile/unauthenticated kill
    kill_token: str = ""

    # Custom themes — directory for user-provided theme overrides.
    # Relative to app root, inside the data mount. Missing dir is silently ignored.
    custom_themes_dir: str = "data/themes"

    # Custom error pages — directory for user-provided error page overrides.
    # Only predefined filenames are loaded: 403.html, 404.html, 500.html.
    # Relative to app root, inside the data mount. Missing dir is silently ignored.
    custom_errors_dir: str = "data/errors"

    # Attachments
    attachment_upload_dir: str = "data/attachments"
    attachment_max_size_mb: int = 50

    # Avatar photos
    avatar_upload_dir: str = "data/avatars"
    avatar_max_size_mb: int = 5
    avatar_max_dimension: int = 256

    # Timer
    timer_max_hours: float = 12.0

    # Cache
    workflow_cache_ttl: int = 3600

    # Recurring tasks — the Celery beat poller that materialises due patterns.
    # ``beat_interval_seconds`` is the scheduler cadence; ``default_lead_time_days``
    # / ``max_lead_time_days`` cap the per-pattern look-ahead the service/schema
    # may enforce; ``batch_size`` bounds how many patterns are loaded per query.
    recurring_tasks_beat_interval_seconds: int = 3600
    recurring_tasks_default_lead_time_days: int = 30
    recurring_tasks_max_lead_time_days: int = 90
    recurring_tasks_batch_size: int = 100

    # Agent sessions
    agent_session_timeout: int = 1800

    # MCP transport session metadata (Redis-backed).
    # Sliding TTL refreshed on every session access; entries are dropped
    # automatically after this many seconds of inactivity.
    mcp_session_ttl_seconds: int = 7 * 24 * 60 * 60

    # i18n
    default_language: str = "en"
    available_languages: list[str] = Field(default_factory=_default_available_languages)

    # Plugins — dotted paths to PluginConfig subclasses
    installed_plugins: list[str] = []

    # Rate limiting — disable for E2E / load testing environments
    rate_limit_enabled: bool = True

    # Trusted proxies — CIDRs whose X-Forwarded-For header is trusted
    trusted_proxies: list[str] = []

    # Search
    search_fts_language: str = "english"
    search_index_comments: bool = True
    search_min_comment_length: int = 20
    search_exclude_bot_comments: bool = True
    # Semantic search through the HNSW vector index: the size of pgvector's
    # candidate list per index scan (``hnsw.ef_search``; higher finds the true
    # nearest neighbours more reliably, at some speed), and how many nearest
    # chunks are fetched per wanted result before visibility filtering.
    search_hnsw_ef_search: int = Field(default=100, ge=10, le=1000)
    search_semantic_overfetch: int = Field(default=4, ge=1, le=50)

    # CPU seconds one Markdown field may use to render before it is abandoned
    # and shown as plain text; the wall-clock ceiling is 4x this. A backstop:
    # the pre-scan in markdown_budget rejects costly text before rendering.
    # The slowest realistic page measured (250 KB) needs about 0.4 s of CPU
    # on a current laptop and the slowest text the pre-scan admits 0.75 s, so
    # 2 s leaves 5x and 2.7x. CPU time is not inflated by load on the host.
    markdown_render_deadline_seconds: float = Field(default=2.0, gt=0)
    # CPU seconds all the Markdown fields rendered for one request may use
    # together (description, comments, history); later fields are shown as
    # plain text once it is spent. 2.5x the per-field deadline.
    markdown_render_request_budget_seconds: float = Field(default=5.0, gt=0)
    # Both limits above are for the machine they were calibrated on, and are
    # multiplied by how much slower this host renders Markdown, measured once
    # per process at first use (at least 1, at most 20). Set this to use a
    # fixed factor instead, e.g. 1 to turn scaling off.
    markdown_render_speed_factor: float | None = Field(default=None, gt=0)

    # Timeouts
    smtp_timeout: int = 30
    webhook_timeout: int = 30

    # SSO (OpenID Connect). Providers are configured in the database; these
    # are the instance-wide limits every provider shares.
    # Break-glass: always show the local login form, whatever the
    # "hide local login" / "auto-redirect" settings say.
    sso_force_local_login: bool = False
    # Allow http:// issuers and endpoints. Only for a disposable test IdP.
    sso_allow_insecure_http: bool = False
    # Timeout for each request to an identity provider.
    sso_http_timeout_seconds: float = Field(default=5.0, gt=0)
    # Leeway for exp / iat / nbf when validating ID and logout tokens.
    # Values above the cap are lowered to it.
    sso_clock_skew_seconds: int = Field(default=60, ge=0)

    @field_validator("search_fts_language")
    @classmethod
    def validate_fts_language(cls, v: str) -> str:
        if v not in _ALLOWED_FTS_LANGUAGES:
            raise ValueError(f"Invalid FTS language {v!r}. Must be one of: {', '.join(sorted(_ALLOWED_FTS_LANGUAGES))}")
        return v

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key_length(cls, v: str) -> str:
        if len(v.encode()) < 32:
            raise ValueError("SECRET_KEY must be at least 32 bytes (64 hex chars recommended)")
        return v

    @field_validator("password_min_length")
    @classmethod
    def validate_password_min_length(cls, v: int) -> int:
        if v < 6:
            raise ValueError("password_min_length must be at least 6")
        return v

    @field_validator("sso_clock_skew_seconds")
    @classmethod
    def cap_sso_clock_skew(cls, v: int) -> int:
        return min(v, SSO_MAX_CLOCK_SKEW_SECONDS)

    @field_validator("legacy_password_hashes_expire_at")
    @classmethod
    def legacy_cutoff_is_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def parse_allowed_hosts(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [h.strip() for h in v.split(",") if h.strip()]
        return v

    @model_validator(mode="after")
    def validate_cors_credentials_no_wildcard(self) -> "Settings":
        """Reject CORS wildcard origin (``"*"``) when credentials are enabled.

        Browsers ignore ``Access-Control-Allow-Credentials: true`` if the
        origin is ``*``, but this misconfiguration can mask bugs and is a
        security code smell.
        """
        if "*" in self.cors_origins and self.cors_allow_credentials:
            raise ValueError(
                "CORS_ORIGINS contains '*' while CORS_ALLOW_CREDENTIALS is True. "
                "Browsers will reject credentialed requests with a wildcard origin. "
                "Either list explicit origins or set CORS_ALLOW_CREDENTIALS=false."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
