"""Application configuration via pydantic-settings."""

from functools import lru_cache
from importlib.metadata import version as _pkg_version

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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

    # Agent sessions
    agent_session_timeout: int = 1800

    # MCP transport session metadata (Redis-backed).
    # Sliding TTL refreshed on every session access; entries are dropped
    # automatically after this many seconds of inactivity.
    mcp_session_ttl_seconds: int = 7 * 24 * 60 * 60

    # i18n
    default_language: str = "en"
    available_languages: list[str] = ["en", "th"]

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

    # Timeouts
    smtp_timeout: int = 30
    webhook_timeout: int = 30

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
