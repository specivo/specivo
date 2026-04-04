# ADR-0004: Backend Conventions

**Date:** 2026-04-04
**Status:** Accepted
**Deciders:** Boris

## Context

Specivo's backend is a FastAPI + SQLAlchemy 2.0 async + Pydantic V2 application with a tiered plugin system (core/pro/enterprise). As the codebase grows, consistent patterns across routing, models, schemas, services, error handling, and testing are essential for maintainability and onboarding. This ADR documents the conventions observed in the codebase and establishes them as the canonical reference.

## Decision

### 1. Project Structure and Layering

The application follows a strict **router -> service -> model** flow. Routers never execute raw SQL or ORM queries directly; they delegate to service classes.

```
specivo/
  api/v1/           # JSON API routers (versioned, included in OpenAPI)
    router.py       # Aggregates all sub-routers under /api/v1
    admin/          # Admin-only endpoints (settings, workflows, audit)
    issues.py       # Domain routers
  web/              # HTML page handlers (excluded from OpenAPI via include_in_schema=False)
    pages/          # Full page renders (Jinja2)
    partials/       # htmx partial responses
    deps.py         # Template loading, optional auth
    router.py       # Aggregates all web sub-routers
  services/         # Business logic — stateless classes, receive AsyncSession as argument
  models/           # SQLAlchemy ORM models
    base.py         # Base, TimestampMixin, LockVersionMixin
  schemas/          # Pydantic V2 request/response models
    common.py       # PaginatedResponse[T], ErrorResponse, IdName, EntityRef
  core/             # Framework plumbing — config, database, security, middleware, plugins
  tasks/            # Celery async tasks (email, webhooks, embeddings)
  testing/          # Shared test infrastructure (fixtures, factories)
  hooks/            # Incoming webhook receivers (e.g. GitHub, GitLab)
  mcp/              # MCP server for AI tool integration
  cli/              # CLI commands (seed, admin)
  templates/        # Jinja2 templates (themes/default/, _shared/)
  static/           # CSS, JS, vendor assets
```

Router mounting order in `main.py` matters: API router first, then plugin routers, then incoming webhooks, then static files, then web pages (catch-all paths last).

### 2. API Endpoint Conventions

**Trailing slashes.** All routes end with `/`. FastAPI's redirect_slashes handles the no-slash case, but canonical URLs always include the trailing slash:

```python
@router.get("/", response_model=PaginatedResponse[ProjectOut])
@router.get("/{key}/", response_model=ProjectOut)
@router.post("/", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
@router.patch("/{key}/", response_model=ProjectOut)
@router.delete("/{key}/", status_code=status.HTTP_204_NO_CONTENT)
```

**HTTP methods and status codes:**
- `GET` returns 200 with the resource or paginated list.
- `POST` returns 201 with the created resource.
- `PATCH` for partial updates (not PUT), returns 200 with the updated resource.
- `DELETE` returns 204 with no body.

**Pagination.** List endpoints accept `offset` and `limit` query parameters and return `PaginatedResponse[T]` from `specivo.schemas.common`:

```python
class PaginatedResponse[T](BaseModel):
    total_count: int
    offset: int
    limit: int
    items: list[T]
```

Defaults and bounds are defined in `specivo.core.constants`: `DEFAULT_PAGE_LIMIT = 25`, `MAX_PAGE_LIMIT = 200`.

**Router instantiation.** Each router module creates a module-level `router = APIRouter(...)` and module-level service instances (`_service = ProjectService()`). The underscore prefix signals these are module-private singletons.

**Dependency injection.** Authentication and database sessions are injected via FastAPI `Depends`:

```python
async def create_project(
    data: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
```

### 3. Error Handling

**`AppError` hierarchy** in `specivo.core.exceptions`:

```python
class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400,
                 field: str | None = None, details: dict | None = None,
                 headers: dict[str, str] | None = None): ...

class NotFoundError(AppError):     # 404, code="not_found"
class PermissionDeniedError(AppError):  # 403, code="permission_denied"
class ConflictError(AppError):     # 409, code="conflict_lock_version"
class UnauthorizedError(AppError): # 401, code="unauthorized"
class ValidationError(AppError):   # 422, code="validation_error"
```

**Unified error response format.** All errors (AppError, HTTPException, RequestValidationError) are normalized into the same JSON envelope by three exception handlers registered in `main.py`:

```json
{
  "errors": [
    {
      "code": "not_found",
      "message": "Project 'XYZ' not found",
      "field": null,
      "details": null
    }
  ]
}
```

The `errors` array supports multiple validation errors in a single response. The `code` field is machine-readable (snake_case); the `message` is human-readable. Validation errors from Pydantic strip the leading `body`/`query`/`path` location prefix and set the `field` to the dotted field path.

**Anti-enumeration.** For private resources, endpoints return 404 (not 403) to prevent existence leakage:

```python
# _require_project_access in api/v1/projects.py
if not project.is_public:
    # ... check membership ...
    raise NotFoundError(f"Project '{project.key}' not found")  # 404, not 403
```

### 4. SQLAlchemy Model Conventions

**Base class** (`specivo.models.base`):

```python
class Base(AsyncAttrs, DeclarativeBase):
    pass
```

All models inherit from `Base`. Two mixins are available:

- `TimestampMixin` -- adds `created_at` and `updated_at` (timezone-aware, `server_default=func.now()`, `onupdate=func.now()`).
- `LockVersionMixin` -- adds `lock_version: int` for optimistic locking via SQLAlchemy's `version_id_col`. Raises `StaleDataError` on concurrent update collision.

Typical model declaration:

```python
class Issue(Base, TimestampMixin, LockVersionMixin):
    __tablename__ = "issues"
    __table_args__ = (
        UniqueConstraint(...),
        Index(...),
        CheckConstraint(...),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
```

**Naming conventions:**
- Table names: plural snake_case (`projects`, `issues`, `enabled_modules`).
- Index names: `ix_{table}_{column}` or `idx_{table}_{purpose}`.
- Unique constraint names: `uq_{table}_{columns}`.
- Check constraint names: `ck_{table}_{purpose}`.
- Column types are always explicit (`mapped_column(Integer, ...)`, not inferred from the Python type alone).

**PostgreSQL-specific features used:**
- `JSONB` for extensible settings/metadata bags.
- `ltree` for hierarchical paths (project tree), with GiST indexes.
- `pgvector` for embedding-based semantic search.
- Expression-based unique indexes for case-insensitive uniqueness: `Index("uq_users_login_ci", func.lower(text("login")), unique=True)`.
- Partial indexes for sparse columns: `Index("ix_users_github_id", "github_id", postgresql_where="github_id IS NOT NULL")`.
- `Numeric(10, 2)` (not Float) for all time/money fields to avoid IEEE 754 rounding.

**FK indexes are always explicit.** PostgreSQL does not auto-index foreign key columns, so every FK has a corresponding `Index(...)`.

### 5. Pydantic Schema Conventions

**Naming pattern:**
- `{Entity}Create` -- POST request body (`ProjectCreate`, `IssueCreate`).
- `{Entity}Update` -- PATCH request body; all content fields optional, `lock_version` required for entities with optimistic locking (`IssueUpdate`).
- `{Entity}Out` -- response schema, with `model_config = {"from_attributes": True}`.
- `{Entity}Filters` -- query parameter model for list filtering (`IssueFilters`).

**Validation.** Field validators use `@field_validator` with `@classmethod`. Normalization (strip, lowercase, uppercase) happens inside validators:

```python
@field_validator("key")
@classmethod
def validate_key(cls, v: str) -> str:
    v = v.strip().upper()
    if not _KEY_RE.match(v):
        raise ValueError("key must be 2-10 uppercase characters...")
    return v
```

Cross-field validation uses `@model_validator(mode="after")`.

**Common schemas** in `specivo.schemas.common`:
- `PaginatedResponse[T]` -- generic paginated list (uses Python 3.12 type parameter syntax).
- `IdName` -- simple id+name pair for lookup values.
- `EntityRef` -- universal entity reference (type, key, id, url, title).
- `ErrorDetail` / `ErrorResponse` -- canonical error schemas.

### 6. Service Layer Patterns

Services are **stateless classes** that receive `AsyncSession` as the first argument to every method. They are instantiated as module-level singletons in both router modules and other services:

```python
class ProjectService:
    async def create(self, session: AsyncSession, data: ProjectCreate, creator_user: User) -> Project: ...
    async def get_by_key(self, session: AsyncSession, key: str) -> Project: ...
    async def list_projects(self, session: AsyncSession, user: User, ...) -> tuple[list[Project], int]: ...
```

**Lazy imports** resolve circular dependencies between services:

```python
# In IssueService
_workflow_service: Any = None

@property
def workflow_service(self) -> Any:
    if self._workflow_service is None:
        from specivo.services.workflow_service import WorkflowService
        self._workflow_service = WorkflowService()
    return self._workflow_service
```

**Service composition over inheritance.** Services compose other services (e.g., `IssueService` uses `JournalService`, `WatcherService`, `NestedSetService`) rather than inheriting from a common base.

**Permission checks** live in a standalone module (`specivo.services.permission_service`), not inside services. Endpoints call `check_permission(user, project_id, permission, db)` explicitly. The permission catalog is a `PERMISSIONS` dict in the same module.

### 7. Authentication and Authorization

Implemented in `specivo.core.security`:

**Dual auth: JWT + API key.** Resolution order:
1. `Authorization: Bearer <token>` header -- if token starts with `spv_`, treat as API key; otherwise JWT.
2. `access_token` cookie -- JWT auth (for browser clients).
3. Neither present -- 401.

**JWT specifics:**
- Algorithm: HS256 (from `specivo.core.constants.JWT_ALGORITHM`).
- Redis blocklist for revoked tokens (`jwt_blocklist:{jti}`).
- Fail-closed on Redis unavailability (denies JWT auth, API key auth remains available).
- Token statuses: `auth_token_expired`, `auth_token_invalid`, `auth_token_revoked`, `auth_account_locked`, `auth_account_deactivated`, `auth_email_not_verified`.

**API keys** use the `spv_` prefix. Locked users can still authenticate via API key (locking is brute-force protection, not agent access control). Deactivated users are blocked from both JWT and API key auth.

**Cookie settings:** httpOnly, SameSite=Lax, Secure when not debug. Refresh token scoped to `/api/v1/auth` path.

### 8. Middleware Stack

All middleware uses **raw ASGI** (not `BaseHTTPMiddleware`) to avoid event loop issues with asyncpg in tests. Stack order (outermost first, as registered in `create_app()`):

1. `RequestIDMiddleware` -- generates/propagates `X-Request-ID`, adds security headers (CSP, X-Frame-Options, X-Content-Type-Options, etc.).
2. `AuditBatchMiddleware` -- initializes `scope["state"]["audit_events"]` list; flushes collected audit events in a single batch INSERT after the response (only when enterprise plugin provides `security_audit_log` feature).
3. `RateLimitHeaderMiddleware` -- copies `X-RateLimit-*` headers from `request.state` onto the ASGI response (survives endpoints that return custom `JSONResponse`).
4. `LocaleMiddleware` -- detects language from cookie (`specivo_lang`) / `Accept-Language` header, activates per-request locale.
5. `TrustedHostMiddleware` (Starlette) -- only added when `allowed_hosts != ["*"]`.
6. `CORSMiddleware` -- standard FastAPI/Starlette CORS handling.

### 9. Rate Limiting

Redis-backed sliding window algorithm in `specivo.core.rate_limit`. Applied as a FastAPI dependency:

```python
@router.post("/login/")
async def login(
    _rl: Annotated[None, Depends(rate_limit("auth_login", max_requests=10, window_seconds=60))],
    ...
):
```

Key format: `rl:{key_prefix}:{identifier}`. Identifier is user ID (if authenticated upstream) or client IP. Graceful degradation: if Redis is down, all requests are allowed (with a warning log). Standard `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` headers. Exceeded limits raise `AppError(429)` with `Retry-After` header.

Trusted proxy handling: only trusts `X-Forwarded-For` when the direct peer IP matches a CIDR in `settings.trusted_proxies`. Never falls back to `is_private` (prevents spoofing by local network attackers).

### 10. Plugin System Architecture

Three-tier architecture: **core** (open source) < **pro** < **enterprise**. Plugin loading order is enforced by tier.

**Plugin contract** (`specivo.core.plugin`):

```python
class PluginConfig(Protocol):
    name: str
    tier: str         # "core", "pro", "enterprise"
    version: str
    def get_models(self) -> list: ...
    def get_routers(self, prefix: str) -> list: ...
    def get_services(self, registry: Any) -> None: ...
    def get_features(self) -> list[str]: ...
    def get_celery_tasks(self, celery_app: Any) -> None: ...
    def get_migration_path(self) -> Path | None: ...
    def get_template_dirs(self) -> list: ...
    def get_static_dirs(self) -> list[tuple[Path, str]]: ...
    def get_static_assets(self) -> dict[str, list[str]]: ...
    def get_locale_dirs(self) -> list[tuple[Path, str]]: ...
    def on_startup(self, app: Any) -> None: ...
```

`BasePluginConfig(ABC)` provides no-op defaults for all hooks. Plugins are specified as dotted paths in `settings.installed_plugins` and loaded via `importlib.import_module`.

**Feature registry** (`specivo.core.features`): plugins register feature flags (e.g., `"security_audit_log"`). Application code checks `has_feature("feature_name")` before enabling optional behavior. Templates access it via Jinja2 global `{% if has_feature("feature_name") %}`.

**Service registry** (`specivo.core.services`): core registers default service implementations; plugins can `override()` them. `register()` raises if the name already exists; `override()` raises if it does not.

### 11. Configuration Management

Single `Settings` class in `specivo.core.config` using `pydantic-settings`:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),  # .env.local overrides .env
        env_file_encoding="utf-8",
        extra="ignore",
    )
    database_url: str       # No default -- must be set
    redis_url: str          # No default -- must be set
    secret_key: str         # No default -- must be set, validated >= 32 bytes
    debug: bool = False
    stealth_prefix: str = ""  # Secret URL prefix for all routes
    # ...
```

Accessed via `get_settings()` which is `@lru_cache`-decorated (singleton). Tests call `get_settings.cache_clear()` after overriding env vars.

Validation rules are strict: `secret_key` must be at least 32 bytes; `cors_origins` with `"*"` and `cors_allow_credentials=True` is rejected; `search_fts_language` is validated against a frozen set of allowed PostgreSQL FTS languages.

Comma-separated list fields (`cors_origins`, `allowed_hosts`) have `mode="before"` validators that split strings.

### 12. Database Session Management

`get_db()` in `specivo.core.database` is a FastAPI dependency that yields a transactional `AsyncSession`:

```python
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

Callers (routers, services) must **not** call `session.commit()` themselves -- the dependency handles it. For multi-step transactions, use `session.begin_nested()` (savepoints).

Engine settings: `pool_pre_ping=True`, `pool_size=10`, `max_overflow=20`, `expire_on_commit=False`.

### 13. Logging

Structured JSON logging in production (`specivo.core.logging`):

```json
{"timestamp": "...", "level": "INFO", "logger": "specivo.services.issue_service", "message": "...", "request_id": "uuid"}
```

Human-readable format in debug mode. `request_id` is propagated from the `RequestIDMiddleware`.

### 14. Async Task Processing

Celery + Redis for background jobs in `specivo/tasks/`. Constants in `specivo.core.constants`:
- `CELERY_MAX_RETRIES = 3`
- Per-task retry delays: email 60s, webhook 30s, embedding 30s, link graph 15s.

Task types: email notifications, webhook delivery, embedding generation, wiki link graph updates, partition management.

### 15. Web Layer (HTML Pages)

Web routers use `include_in_schema=False` to exclude from OpenAPI docs. Authentication is optional (via `get_current_user_optional`), with redirect to `/login/` for unauthenticated users on protected pages.

Template rendering uses `Jinja2Templates` with a `ChoiceLoader` for theme support (resolution: custom theme -> default theme -> shared templates).

### 16. Testing Conventions

**Test isolation: transaction rollback.** Each test runs inside a top-level transaction that is rolled back after the test completes (~1ms vs ~300ms for TRUNCATE). The `db_session` fixture uses savepoints so `session.commit()` within test code creates a savepoint instead of committing the outer transaction.

**Markers** (from `pyproject.toml`):
- `unit` -- pure logic tests, no database.
- `integration` -- API endpoint tests, requires database.
- `service` -- service layer tests, requires database.
- `slow` -- tests > 5 seconds.
- `pro` / `enterprise` -- auto-skipped when the corresponding plugin is not installed.
- `serial` -- cannot run under pytest-xdist (shared state like Redis rate limits).
- `e2e` -- browser tests requiring a running server.

**Client fixtures** in `specivo.testing.conftest_base`:
- `client` -- unauthenticated HTTPX `AsyncClient`.
- `auth_client` -- pre-authenticated as a regular user via JWT (user accessible via `auth_client.state.user`).
- `admin_client` -- pre-authenticated as an admin user via JWT.
- `agent_client` -- pre-authenticated as a service account via API key.
- `unauth_client` -- no DB override, for testing public endpoints.
- `db_session` -- direct async DB session for test data setup.

**Factories** in `specivo.testing.factories/` using `factory_boy`. Password hashing is done once at import time to avoid bcrypt cost per test:

```python
_TEST_PASSWORD_HASH = hash_password("testpassword")  # Computed once

class UserFactory(factory.Factory):
    class Meta:
        model = User
    login = factory.Sequence(lambda n: f"user{n}")
    password_hash = _TEST_PASSWORD_HASH
```

Variants: `AdminUserFactory`, `ServiceAccountFactory`.

**Test parallelism:** `addopts = "-n auto --dist worksteal"` in `pyproject.toml` -- tests run in parallel via pytest-xdist by default. Tests marked `serial` are excluded from parallel runs.

**Plugin isolation:** `_restore_plugin_manager` autouse fixture restores the global `PluginManager` singleton after each test. `pytest_collection_modifyitems` auto-skips `pro`/`enterprise` tests when plugins are not installed.

### 17. Code Style

**Tooling:**
- `ruff` -- linter and formatter, `target-version = "py312"`, `line-length = 120`, rules: `["E", "F", "I", "N", "W", "UP"]`.
- `mypy` -- `python_version = "3.12"`, `strict = false`, `warn_return_any = true`.
- Tests relax `F841` (unused variables) and `N806` (PascalCase locals).

**Naming:**
- Module-private singletons: `_service = ProjectService()`, `_svc = ProjectService()`.
- Constants: `UPPER_SNAKE_CASE`, grouped in `specivo.core.constants`.
- Private helpers: `_build_path()`, `_require_manage()`, `_issue_out()`.
- Allowed-value sets: `frozenset` (immutable).

**Imports:**
- `from __future__ import annotations` at the top of most modules (deferred evaluation).
- `TYPE_CHECKING` guard for imports only needed by type checkers.
- Lazy imports inside functions/properties to break circular dependencies.

**Docstrings:**
- Module-level docstring on every `.py` file describing its purpose.
- Class and method docstrings for public API.
- Inline comments for non-obvious behavior, security rationale, and PostgreSQL-specific gotchas (e.g., "PostgreSQL does NOT auto-index FK columns").

### 18. Audit Logging

Event types and actions are `StrEnum` constants in `specivo.services.security_audit_service` (`AuditEvent`, `MemberAction`). Never hardcode strings — import the enum.

```python
from specivo.services.security_audit_service import AuditEvent, MemberAction

# Good — uses enum
await audit.log_member_change(session=db, action=MemberAction.ADDED, ...)

# Bad — hardcoded string
await audit.log_member_change(session=db, action="added", ...)
```

**Rules:**
- Failed permission attempts must be logged before raising 403.
- Audit writes in error paths must `commit()` before re-raising, otherwise `get_db` rollback discards them.
- All events go to `security_audit_logs` (partitioned by month, with a default catch-all partition).

## Consequences

**Positive:**
- Consistent patterns across all modules reduce cognitive load -- once you have seen one router/service/model, you know the structure of all of them.
- The `AppError` hierarchy with uniform JSON envelope means clients need a single error-handling path.
- Transaction-rollback test isolation keeps the test suite fast even as it grows.
- The plugin system allows pro/enterprise features to extend the core without modifying it.
- Stateless services with explicit session passing make testing straightforward (no hidden global state).

**Negative:**
- Module-level service singletons (`_service = IssueService()`) import eagerly at module load time, which can cause import-order issues during testing. The lazy import pattern is the established workaround.
- The `get_db()` dependency committing on clean exit means services must not call `session.commit()` -- a deviation from some common patterns that must be clearly documented.
- Raw ASGI middleware is more verbose than `BaseHTTPMiddleware` but is necessary for asyncpg compatibility.

## Not Chosen

- **BaseHTTPMiddleware** -- creates an extra task group that causes "Future attached to a different loop" errors with asyncpg in tests. Raw ASGI middleware avoids this.
- **Global session / unit-of-work pattern** -- rejected in favor of the FastAPI dependency-injected session, which provides clear transaction boundaries per request.
- **Service base class** -- composition is preferred. A common base class was considered but would add coupling between unrelated services without meaningful code reuse.
- **PUT for updates** -- PATCH is used exclusively since all updates are partial.
