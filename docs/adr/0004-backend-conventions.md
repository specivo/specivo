# ADR-0004: Backend Conventions

**Date:** 2026-04-04
**Revised:** 2026-09-08 — recorded the three middleware added since (CSRF, token refresh, SQL
debug), the `Permission` enum, and project membership becoming principal-shaped; corrected the
xdist distribution mode in §16.
**Revised:** 2026-10-26 — recorded role visibility settings and the time tracking permissions,
the password hasher and import source plugin hooks, and that the plugin contract lists them.
**Revised:** 2026-10-28 — removed the import source plugin hook and the `redmine` test marker with
the direct Redmine import (imports now go through bundles, see `docs/import-bundle/v1.md`); recorded the `sso_idp`
marker.
**Revised:** 2026-10-29 — recorded how an API key's project limit is enforced (§7).
**Revised:** 2026-10-31 — recorded the SSO session check at the IdP and its session columns (§7).
**Revised:** 2026-11-05 — recorded that metadata schemas and workflow field rules judge the change a
write makes, not the state of the issue (§6).
**Revised:** 2026-11-06 — recorded how MCP transport sessions live and end (§4).
**Revised:** 2026-11-05 — recorded the email change audit events and what their details hold (§18).
**Revised:** 2026-11-06 — recorded the money convention (§4), the currency audit events and the repository provider change audit event (§18).
**Revised:** 2026-11-06 — recorded exchange rates and the conversion contract (§4), and the exchange rate and development link detach/move audit events (§18).
**Revised:** 2026-11-06 — recorded AI tools and personal AI subscriptions, their coverage and proration rules (§4), and the AI subscription audit events (§18).
**Revised:** 2026-11-06 — recorded AI spend reports: grouping, conversion, seat allocation and who sees seat cost (§4), and the route order of `/ai-usage/report/` (§4).
**Status:** Accepted
**Deciders:** Boris

## Context

Specivo's backend is a FastAPI + SQLAlchemy 2.0 async + Pydantic V2 application with a plugin system for password hashers, routers, services and templates. As the codebase grows, consistent patterns across routing, models, schemas, services, error handling, and testing are essential for maintainability and onboarding. This ADR documents the conventions observed in the codebase and establishes them as the canonical reference.

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
    db: AsyncSession = DB_SESSION,
) -> ProjectOut:
```

Take the session as `db: DbSession` (an `Annotated` alias), or as `db: AsyncSession = DB_SESSION` where the parameter follows others with defaults. Never write a bare `Depends(get_db)` -- see section 12.

**Published contracts.** An API that third parties implement on either side (today the AI usage ingest, `docs/contracts/ai-usage/v1/`; the import bundle, `docs/import-bundle/v1/`, follows the same idea) is a versioned folder in `docs/`: JSON Schemas (draft 2020-12, `$id` under `https://specivo.io/schemas/<name>/v<N>/`), a README written for implementers, and `examples/` with an `expected.json` of the server's answers. The schemas the server validates against are a copy inside the package (`specivo/data/contracts/...`), and a unit test keeps the two identical; another test validates every example and replays the valid ones through the endpoint. Within a major version changes are additive only; the server ignores request properties it does not know (and says so in a warning) and clients ignore unknown response properties. A breaking change is a new version on the same endpoint, selected by a `schema` property in the body, and the server keeps the current and the previous major version.

**Batch ingest endpoints.** A batch `POST` (the AI usage ingest) answers **200** with one result per record whenever the envelope is readable, even if every record was rejected; records are processed one by one, each in its own savepoint, so a rejected record leaves nothing behind. Envelope problems are **400** with their own codes (`invalid_json`, `invalid_batch`, `unsupported_schema`) rather than 422, because the body is read and validated against the published schema by hand, not by Pydantic; a body over the size or record limit is **413**, and the per-account rate limit is **429** with `Retry-After` (`enforce_rate_limit`). Per-record errors are contract codes (`invalid_record`, `forbidden`, `not_found`, ...), mapped from the services' `AppError` codes in one table. Because external clients post to the URL the contract spells, such a route is registered both with and without the trailing slash (the slash-less one hidden from OpenAPI) instead of relying on a redirect that a `POST` client may not follow.

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
- Partial indexes for sparse columns: `Index("ix_user_group_members_managed_by_provider_id", "managed_by_provider_id", postgresql_where=text("managed_by_provider_id IS NOT NULL"))`.
- `Numeric` (never Float) for time and money fields to avoid IEEE 754 rounding.

**Money.** Every amount is an exact `NUMERIC` plus a sibling `<name>_currency` `CHAR(3)` column referencing
`currencies.code` (ISO 4217, seeded from `specivo/data/iso4217.csv`; rows are deactivated, never deleted).
Declare the pair with `specivo.models.money` (`amount_column`, `currency_column`, `money_pair_constraints`):
computed amounts `NUMERIC(20, 8)`, unit prices `NUMERIC(18, 6)`, exchange rates `NUMERIC(24, 12)`. In Python an
amount is `specivo.core.money.Money` (immutable `Decimal` + code; arithmetic within one currency only, mixing
raises `CurrencyMismatchError`; floats are refused). Computed values are stored unrounded and rounded half up to
the currency's minor units on display and in posted totals (`Money.quantize`, `Money.format`). Values people type
are validated to the minor units on input. Over the API an amount is a string (`MoneyIn` / `MoneyOut` in
`specivo.schemas.money`), never a JSON number. A currency chosen now must be enabled
(`CurrencyService.require_selectable`); a stored reference to a currency disabled since stays valid. The reporting
currency is the instance setting `reporting_currency` (default USD) unless `projects.reporting_currency_code`
overrides it; resolve it with `reporting_currency_for(session, project)`.

**Exchange rates and conversion.** Rates live in `exchange_rates` (`1 base_code = rate quote_code` on `rate_date`, a
`DATE`; `NUMERIC(24, 12)`; source `manual`, `csv` or `ecb`; one row per day, pair and source; the inverse is never
stored). Convert only through `ExchangeRateService` (`convert`, `convert_many`, `summarize`), which loads the rates a
batch needs in at most two queries and resolves them in memory with `specivo.core.exchange.RateBook`: newest date on
or before the amount's date within `exchange_rate_max_age_days` (policy `entry_date`, the default; or `latest`, or a
fixed date), then same-day precedence manual > csv > ecb, the stored pair before its inverse, and triangulation
through `exchange_rate_pivot` (EUR) only when neither direction has a rate. A missing rate is a `RateMissing` result
(`no_rate` / `stale`), never zero: reports total the `Converted` results and list the rest per currency and reason
(`ConversionSummary`, `ConversionSummaryOut`). Never convert per row in a loop of queries, and never through `float`.
The optional ECB fetch is off unless `exchange_rates_fetch_enabled` is `"true"`. It goes through
`specivo.services.exchange_rate_feed`: every hop resolved, checked and pinned (private ranges only with
`EXCHANGE_RATE_FEED_ALLOW_PRIVATE_NETWORKS`, metadata never), environment proxies chosen per hop inside that guard
(`trust_env=False`; unresolvable targets refused unless `EXCHANGE_RATE_FEED_ALLOW_UNRESOLVED_VIA_PROXY`; the proxy
must enforce egress rules, as it resolves again), one total deadline, strict UTF-8 with no DTD, and error messages that never echo the response.

**AI model prices.** `ai_model_prices` is a price **history**: one model's per-1M-token prices (`NUMERIC(18, 6)`,
any active currency) during a half-open period `[effective_from, effective_to)` (`TIMESTAMPTZ`, `NULL` end = open),
from source `seed` (the bundled LiteLLM snapshot in `specivo/data/ai_model_prices.json`, plus the optional
hand-kept `ai_model_prices_history.json`) or `admin`. A `btree_gist` exclusion constraint keeps the active periods
of one provider, model and source from overlapping; the trigger `ai_model_prices_immutable` refuses any UPDATE of
prices, currency, identity or start. A change closes the open period and inserts a new one in one transaction
(`AiModelPriceService.create`); the seed loader (`specivo.core.ai_pricing_seed.plan_seed`, applied by
`specivo.services.ai_model_price_seed.apply_seed` in migration 0061, at worker start and on demand) only appends
periods and never touches `admin` rows. Resolve a price only through `AiModelPriceService.resolve(model, at,
provider)`: names normalised (case, provider prefixes), model before alias, `admin` before `seed`, at an aware
instant compared in UTC; no match is `None` (unpriced), never zero and never the latest price. Compute costs only
with the pure `specivo.core.ai_pricing.price_usage(tokens, long_context_tokens, price)` (exact `Decimal`, partial
when a class with tokens has no price). A table that prices with these rows registers a `PriceReference` so
referenced rows cannot be deleted or ended before their entries.

**AI tools and subscriptions.** `ai_tools` is a lookup keyed by slug (`^[a-z0-9][a-z0-9-]{1,49}$`), seeded with the
common coding agents; a slug a reporter sends that is not listed is created on first use, inactive, only through
`AiToolService.ensure`. Tools are never deleted. `ai_subscriptions` holds personal flat-rate plans: `amount` +
`currency_code` (`NUMERIC(20, 8)`, typed, so validated positive and to the currency's minor units; a new or changed
currency must be enabled), `billing_period` `month` or `year`, and `starts_on` / `ends_on` as **UTC days, both
inclusive** (`ends_on` `NULL` = open): a subscription covers `[starts_on 00:00Z, ends_on + 1 day 00:00Z)`, and an
instant is matched by its UTC date, the rule `priced_at` and the UTC-day usage buckets follow. `tool_slugs` lists the
tools a plan covers; empty means the tools whose `default_provider` is the plan's provider. Resolve coverage only
through `AiSubscriptionService.subscription_for(user_id, tool, provider, at)`: a plan listing the tool first, then a
provider plan when the usage's provider (or the tool's default) is the plan's and the tool belongs to that provider;
within a step the latest start, then the newest row. Seat cost goes through the pure
`specivo.core.ai_subscriptions.prorated_fee` / `subscription_charges` (a yearly fee divided by 12, then
`fee x covered days / days in the month`, exact `Decimal`, one division) and `AiSubscriptionService.seat_cost` /
`seat_charges_between`; amounts stay in each plan's currency. People manage their own subscriptions while the setting
`ai_subscriptions_self_service` is on (default); administrators manage everyone's; nobody else sees one; service and
system accounts hold none; the `/my/` routes are account-wide (`get_account_user`). Plan presets
(`PLAN_PRESETS`) are suggestions only.

**AI usage entries.** `ai_usage_entries` holds one bucket of usage per session x model x **UTC day**, logged in a
user's name (`user_id`, on behalf of) and written by `reporter_user_id` / `reporter_api_key_id` with a `source`
(`manual`, `api`, `mcp`, `reporter`, `import`). The reporter columns, the source and the client name and version never
change. Reporter and import entries carry an `idempotency_key`, unique per target user (`uq_ai_usage_entries_user_key`),
are upserted with full bucket totals (`AiUsageService.upsert`: `created`, `updated` with `revision + 1`, or `unchanged`
with no write; a key reported by another account is 409 `key_conflict`; an existing entry is authorized with
`require_can_log` against its **stored** project and user, never the project the replay names, and a replay naming a
different project than the stored one is 409 `ai_usage_attribution_conflict`: reporters attribute unassigned entries
only, people move entries), keep their token, model and tool columns
(only `project_id`, `issue_id` and `comments` change; 422 `ai_usage_field_locked`), are excluded rather than deleted
(`excluded_at` survives upserts; admins may hard-delete), and keep an attribution once set. Costs are a **snapshot at
write time** (`AiUsageService.apply_costs` + the pure `specivo.core.ai_usage.compute_costs`): priced at `priced_at`
(`started_at`, else 00:00 UTC of `usage_date`; `started_at` must fall on that UTC day) with the price
`AiModelPriceService.resolve` returns, stored with `price_id` (FK `RESTRICT`, registered as a `PriceReference`). The
marginal cost follows the cost type: `api` = the reported cost if given (`cost_basis` `reported`, `manual` when a person
typed it), else the API equivalent; `subscription` / `free` = 0; `local` = 0 or the local price row; `unknown` = NULL.
A missing cost type defaults to `subscription` when `AiSubscriptionService.subscription_for` covers the usage, else
`unknown`. Prices added or edited later never change an entry; only `AiUsageService.recompute_range` (the Celery task
`specivo.tasks.ai_usage.recompute_ai_usage_costs`, started from `POST /api/v1/admin/ai-usage/recompute/` or the offer on
the admin price page after a past period is entered) re-prices entries, each at its own `priced_at`, bumping `revision`
where costs change. No prompt, code or file path is ever stored.

**Agent sessions.** `agent_sessions` holds one row per activity window of an API key (requests less than
`agent_session_timeout` apart), started or extended by `AgentSessionService.get_or_create_session` on every
API-key request and MCP tool call. `client_name` / `client_version` name the client, never a model: the MCP
`clientInfo` (published per request by `ClientInfoMiddleware` in `specivo.mcp.client_context`), else the first
User-Agent product; a window keeps the first client it sees. `issue_id` is set only through MCP, by
`record_working_issue` after a tool moves an issue into an `active` status, assigns it to the calling account or
starts a timer on it (latest wins), and only for an issue that account can see. Tools reach the current window
through the `agent_session_id_var` context variable that `_get_session_and_user` sets for the tool call, not through
their signatures, and both writes run in a savepoint so a failure never fails the tool.
`AgentSessionService.issue_for(user_id, started_at, ended_at)` returns the issue of the window overlapping a time
range longest (ties: most recently active); it is an attribution hint, so callers still check they may log there.
The id of the API key that authenticated a tool call is published the same way (`mcp_api_key_id_var`), for tools
that record their reporting key (`specivo_log_ai_usage`).

**MCP transport sessions** are a different thing from agent sessions: the `Mcp-Session-Id` of a Streamable HTTP
connection. Their Redis entry (`specivo.mcp.session_store`, sliding TTL `mcp_session_ttl_seconds`) is the source of
truth; the SDK's in-memory transport is a cache that `PersistentStreamableHTTPSessionManager` rebuilds from it after
an idle reap (`mcp_session_idle_timeout_seconds`), a dropped client stream or a restart, born initialized and only for
the API key that opened it. A session that is gone (expired, ended by DELETE, unknown, or another key's) is answered
with HTTP 404, the spec's signal to re-initialize, never with a JSON-RPC error: a `-32602` reads as a caller mistake
and no client recovers from it. The legacy SSE transport has no session continuity (the SDK mints an id per GET), so
each SSE stream is served born initialized too (`serve_initialized_connection`): a client that reconnects its
stream does not repeat `initialize`.

**AI usage surfaces.** The REST routes (`api/v1/ai_usage.py`) mirror time entries: `/projects/{key}/ai-usage/`,
`/my/ai-usage/` (the caller's own entries, unassigned ones included) and `/ai-usage/{id}/` with `exclude/` and
`include/`; the router is included after the ingest router so `/ai-usage/ingest/` is matched first. An entry written
there has the source `api` with an API key and `manual` without one; the MCP tools (`specivo.mcp.ai_usage_tools`, a
module of its own beside `tools.py`) write `mcp`. Both log through `AiUsageService.create_for`, which resolves
`on_behalf_of` with `resolve_target`, and render pages with `AiUsageService.references`: users, project keys and the
issue keys the reader may see in three queries; an issue the reader cannot see shows as no issue. List filters live
in `AiUsageService.list_entries` (a model filter matches its price aliases; an unknown login filter matches nothing
rather than saying it is unknown).

**AI spend reports.** Reports of AI usage go only through `AiSpendReportService.report`
(`specivo.services.ai_spend_report_service`): one project (anyone who reaches it; rows only from the entries
`AiUsageService` lets them read, excluded ones never) or the whole instance (`is_global_admin` only), grouped by any
of `GROUP_BY` plus a `day` / `week` (ISO, from Monday) / `month` bucket of the UTC usage day, sorted by the grouping
key unless the caller asks for spend (no rankings by default). Every amount is converted in **one**
`ExchangeRateService.convert_many` call into the reporting currency (`reporting_currency_for`, or an explicit active
currency) under the report's rate policy; results are folded per row with `summarize`, so unconverted amounts stay
listed and the rates used are reported. Seat cost is computed at report time, never stored: per user, month and
subscription the month's `prorated_fee` is spread over the entries with `cost_type = 'subscription'` and that
`subscription_id`, weighted by `api_equivalent` converted into the plan's currency at each entry's date, over **all**
of the user's counted entries of the month (whatever the viewer sees or the period covers, so a share is stable);
a month without such priced entries leaves the fee **unallocated** (prorated to the days the period covers), shown
only in instance reports without a tool, model or provider filter. Seat fees in another currency are converted at
the month's last day (today for the running month, `SEAT_RATE_DAY`). A project report shows seat cost only to a
viewer for whom `sees_every_entry` holds (an admin, or `ai_usage_visibility = 'all'` with no counted entry of the
range hidden from them); no report names a plan or shows a fee as such. The query count does not grow with the data
(one grouped query, the subscriptions, the weights, label lookups, at most three statements per conversion target).
The REST routes (`/projects/{key}/ai-usage/report/`, `/ai-usage/report/`, `format=csv`) are included **before**
the entries router so `/ai-usage/report/` is not read as an entry id; CSV cells that could start a spreadsheet
formula are prefixed with `'`.

**FK indexes are always explicit.** PostgreSQL does not auto-index foreign key columns, so every FK has a corresponding `Index(...)`.

**Principals.** A `members` row is held by exactly one principal — a user (`user_id`) or a user group (`group_id`) — with the other column NULL and `ck_members_one_principal` enforcing it. Roles hang off the row through `member_roles` identically either way, so nothing downstream of a resolved role knows which kind granted it. Uniqueness per project is two plain `UniqueConstraint`s rather than partial indexes: PostgreSQL treats NULLs as distinct, so each ignores the other principal's rows.

`AgentGroup` is unrelated — an AI-agent access-policy construct with no bearing on project membership. The membership principal is `UserGroup`.

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

**Permission checks** live in a standalone module (`specivo.services.permission_service`), not inside services. Endpoints call `check_permission(user, project_id, permission, db)` explicitly.

The catalogue is the `Permission` `StrEnum` in that module; each member's value is the string stored in `roles.permissions` JSONB, so the constants are drop-in replacements for the literals. **Use the enum, not a string literal.** The `PERMISSIONS` dict maps the same values to human labels for the admin role editor.

`get_user_roles(session, user, project)` is the one role resolution behind permission checks, issue visibility and (mirrored in SQL) search visibility: a membership's roles; otherwise, on a public project, the seeded read-only Non member role (`roles.builtin = 1`) plus the project's transient anonymous role when the instance switch is on; for the anonymous user, only that transient role, capped at `view_issues` / `view_wiki` inside `check_permission`. Resolved roles are cached in `session.info`, so the cache lives exactly as long as a request's session. There are no hardcoded `is_public` access branches. `member_principal_clause(user_id)` is the single definition of "this `members` row grants this user access" — see §4 on principals. Any membership read that feeds an access decision uses that clause; a row looked up in order to be *edited* (add, update or remove one user's roles) must stay user-only, or the edit lands on a group and reaches everyone in it.

Each role also carries `users_visibility` and `time_entries_visibility` beside `issues_visibility`, and the widest value across a user's roles wins. For time entries only roles that grant `view_time_entries` count: a user with none reads no entries in the project, their own included. Time access is decided in `TimeEntryService`, not inline in endpoints: `visible_entries_clause` / `visible_entries_clause_any` scope every reader (list, single entry, issue and version totals, billing, the dashboard feed), `require_can_log` guards every entry written on a user's behalf (timers included), and `can_edit` covers edits and deletes. `create` checks nothing itself, because the importer writes entries for their original authors.

AI usage mirrors this in `AiUsageService` with `view_ai_usage`, `log_ai_usage`, `manage_ai_usage`,
`log_ai_usage_for_others` and `roles.ai_usage_visibility` (`all` / `own`, default `own`; the seeded Manager is `all`),
with three differences. People always read the entries logged in their own name, without any permission and unassigned
ones included; a reporter reads what it reported; and entries without a project are otherwise visible to admins only.
Entries on issues the reader cannot see are hidden unless they are the reader's own. A project-limited key sees
unassigned entries only where it is the reporting key (`ProjectScope.key_id`, `limited_key_id()`). Logging in another
user's name needs `log_ai_usage_for_others` in the project and the target must be an active member there; unassigned
entries for someone else are for admins only (`require_can_log`). Moving an entry to another project (or making it
unassigned) runs the same `require_can_log` in the destination in the entry's user's name, so someone else's entry needs
`log_ai_usage_for_others` there.
**`on_behalf_of` never enumerates logins.** Whoever may not log for others in the project at hand (an admin, or
`log_ai_usage_for_others` there; unassigned: admins only) gets one refusal (`on_behalf_refused()`, 403
`ON_BEHALF_REFUSED`, field `on_behalf_of`) for every login but their own: unknown, inactive, non-member and permitted
targets alike. `require_can_log` checks the on-behalf permission before the target's validity and membership, and
REST and MCP resolve the login with `AiUsageService.resolve_target`, which looks the login up only after that check.
The ingest validates record content before the target, refuses a sender without the right in any project before
looking anything up, and, for a sender who may not tell targets apart in the record's explicit project, turns every
rejection of a record naming another user (explicit hints, key conflicts, database errors included) into that refusal,
without the warnings found on the way.
The per-project form of both entry kinds' cross-project visibility is read once by
`permission_service.entry_visibility_projects(session, user, permission, visibility_attribute)`; use it rather than
another membership query.

**Validation judges the change, not the state.** Metadata schemas and workflow field rules are
enforced in the service layer on every write path (REST, web, MCP), and they look only at what the
request changes, so an issue holding an old invalid value stays editable and tightening a rule never
traps existing data:

- `MetadataSchemaService.validate_metadata(..., previous=...)` diffs the top-level keys against the
  stored metadata (`previous=None` means create, where every key counts and root `required` applies)
  and keeps only `jsonschema` errors that touch a changed key. With nothing changed it reads no schema.
  The error is a `ValidationError` with `field="metadata.<key>"`, a translated prefix and the
  `jsonschema` reason in English.
- `WorkflowService.validate_field_rules` runs on every update against the status the issue ends up in:
  `readonly` refuses a requested value that differs from the current one, `required` checks the value
  after the update on a status change and otherwise refuses only a clear. A tracker without rules costs
  one `EXISTS` query.
- Paths that carry values decided elsewhere skip metadata validation explicitly with
  `IssueService.create(..., validate_metadata=False)`: the importer (history is kept, and mismatches
  are reported as warnings) and recurring generation (the template was validated when the pattern was
  saved). Moving an issue and changing its tracker re-validate nothing.

### 7. Authentication and Authorization

Implemented in `specivo.core.security`:

**Dual auth: JWT + API key.** Resolution order:
1. `Authorization: Bearer <token>` header -- if token starts with `spv_`, treat as API key; otherwise JWT.
2. `access_token` cookie -- JWT auth (for browser clients).
3. Neither present -- 401.

**JWT specifics:**
- Algorithm: HS256 (from `specivo.core.constants.JWT_ALGORITHM`).
- Redis blocklist for revoked tokens (`jwt_blocklist:{jti}`).
- `sid` claim = `refresh_tokens.session_id`. A session (one login) keeps its id across refresh rotation; rotation copies every session column (`session_id`, `auth_method`, `user_identity_id`, `idp_sid`, `id_token_encrypted`, `idp_refresh_token_encrypted`, `idp_checked_at`, `absolute_expires_at`) and refuses to refresh past `absolute_expires_at`. An SSO session is renewed only after its IdP accepted the stored refresh token (`specivo.services.sso.session_check`, at most once per `SSO_IDP_RECHECK_MINUTES`); that token is sealed for the issuer, token endpoint and client that issued it and is never sent anywhere else. The IdP is asked before the session is locked, with the read transaction already committed (no pooled connection, advisory lock or claimed row is held during network I/O), under a Redis guard per refresh row (`sso_idp_recheck:{row id}`) so the IdP's refresh token is presented once; its answer is applied only after the row was claimed. Never call an external service while holding the session lock.
- Session revocation goes through one primitive, `SessionService.revoke(db, session_ids, reason=...)` (`specivo.services.session_service`): it deletes the session's refresh rows and sets `revoked_session:{sid}` in Redis for one access-token lifetime, so the session's access tokens are refused on the next request. Logout (web and API), logout-all, `DELETE /auth/sessions/{id}` and the password-change sweep use it; `revoke_user_sessions`, `revoke_by_idp_sid` and `revoke_by_identity` cover bulk and SSO cases. Do not delete `refresh_tokens` rows directly to end a session. Refresh and revocation both take a per-session advisory lock (`SessionService.lock_sessions`); refresh claims the old row with `DELETE ... RETURNING` and is refused when that finds nothing or when `revoked_session:{sid}` is set (fail closed). Sign-out acts on an access token's `sid` only if the token is unexpired or belongs to the user of the refresh token presented with it.
- `_authenticate_jwt` checks `jwt_blocklist:{jti}` and `revoked_session:{sid}` in one `EXISTS`. A token without `sid` (minted before the claim existed) is checked by `jti` only and stays valid until it expires.
- Fail-closed on Redis unavailability (denies JWT auth, API key auth remains available).
- Token statuses: `auth_token_expired`, `auth_token_invalid`, `auth_token_revoked`, `auth_account_locked`, `auth_account_deactivated`, `auth_email_not_verified`.

**API keys** use the `spv_` prefix. Locked users can still authenticate via API key (locking is brute-force protection, not agent access control). Deactivated users are blocked from both JWT and API key auth.

**Session-only admin routes.** An admin route that hands out a way to sign in or to act as someone (set a password, create any account other than a service account, unlock/reactivate an account, mint or re-enable API keys or credentials for another user, change the SSO configuration) depends on `require_admin_session_api` (`specivo/api/v1/admin/__init__.py`) instead of `require_admin_api`: API-key auth (`request.state.api_key_id` set) gets 403 `api_key_not_allowed`. The matching web forms use `require_admin_session` (`specivo/web/pages/admin.py`), because the web layer also accepts a Bearer key and a request without a session cookie is not CSRF-checked. Taking access away (lock, deactivate, revoke) and ordinary administration stay key-accessible. `tests/integration/test_admin_api_key_gates.py` lists the gated routes; add a new credential-minting route there.

**Cookie settings:** httpOnly, SameSite=Lax, Secure when not debug. Refresh token scoped to `/api/v1/auth` path.

**Project-limited API keys.** A key whose `scopes` holds `{"projects": [...]}` reaches only those projects, for REST, the web layer and MCP alike. Authentication resolves the limit to project ids once (`activate_api_key_scope`, called by `authenticate_request` and by the MCP tool session helper) and holds it in a context variable for the rest of the request or tool call; `ApiKeyScopeMiddleware` and the MCP helper reset it afterwards. Access decisions read it from there rather than taking it as an argument, so a call site cannot forget it: `get_user_roles` returns no roles outside the limit, `check_permission` denies outside it (and denies project-less permissions) before the admin bypass, `ProjectService.get_by_key` / `require_project_access` answer 404, and the SQL builders (`accessible_projects_clause`, `visible_issues_clause`, `visible_entries_clause_any`, the search services' project filter) narrow to it. An admin shortcut that grants a whole project uses `admin_reaches(user, project_id)`; one that grants the whole instance uses `is_global_admin(user)`, which is false for a limited key, so the admin API, project creation and similar refuse it. Account-wide routes (API keys, sessions, password) depend on `get_account_user`, which refuses a limited key with 403. The limit is validated once, at creation (`ApiKeyService.create_key`, schema `ApiKeyScopes`): `{"projects": [...]}` naming 1 to 200 projects by key or id, nothing else; an empty list, other keys, a bare list or an unknown project answer 422 on `scopes` (never read `[]` as "unlimited"). A self-service key may name only projects its owner can see, and a missing project and an invisible one get the same message; the admin route for another user checks existence only. It is stored as `{"projects": [ids]}`, so a project key rename does not change it, and fixed after creation. Responses carry `project_keys` (`project_keys_by_api_key`, one query per list). The read path stays tolerant: limits stored by key, a bare list (not limited) and JSON `null` all read without error, and an unreadable limit fails closed. See `specivo/services/api_key_scope.py`.

### 8. Middleware Stack

All middleware uses **raw ASGI** (not `BaseHTTPMiddleware`) to avoid event loop issues with asyncpg in tests. Stack order (outermost first, as registered in `create_app()`):

1. `RequestIDMiddleware` -- generates/propagates `X-Request-ID`, adds security headers (CSP, X-Frame-Options, X-Content-Type-Options, etc.).
2. `CSRFMiddleware` -- double-submit cookie CSRF protection: sets a signed, non-HttpOnly `csrf_token` cookie on GET responses and requires a matching `X-CSRF-Token` header on POST/PATCH/PUT/DELETE. Exempt: requests carrying an `X-API-Key` or `Authorization` header (not cookie-based); the exact auth endpoints `login/`, `refresh/`, `forgot-password/` and `reset-password/` (listed one by one in `_CSRF_EXEMPT_PATHS`, never as an `/api/v1/auth/` prefix, so a new auth route is protected by default); and the MCP mount, which accepts Bearer API keys only.
3. `AuditBatchMiddleware` -- initializes `scope["state"]["audit_events"]` list and binds the request's audit context (client address, user agent, request id); flushes collected audit events in a single batch INSERT after the response, in a session of its own. A failed flush is logged and never fails the response.
4. `SQLDebugMiddleware` -- per-request SQL profiler adding `X-SQL-Query-Count` and friends. Registered only when `settings.debug`; the middleware itself does not check the flag.
5. `RateLimitHeaderMiddleware` -- copies `X-RateLimit-*` headers from `request.state` onto the ASGI response (survives endpoints that return custom `JSONResponse`).
6. `TokenRefreshMiddleware` -- when `get_current_user_optional()` has transparently rotated an expired access token using the refresh cookie, it parks the new pair on `scope["state"]["refreshed_tokens"]`; this middleware appends the `Set-Cookie` headers for them.
7. `LocaleMiddleware` -- detects language from cookie (`specivo_lang`) / `Accept-Language` header, activates per-request locale.
8. `TrustedHostMiddleware` (Starlette) -- only added when `allowed_hosts != ["*"]`.
9. `CORSMiddleware` -- standard FastAPI/Starlette CORS handling.

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

Everything ships in Specivo itself; there are no editions and no feature flags. Plugins add things core does not have (legacy password hashers, extra routers, services, templates, static files, translations) and load in the order `INSTALLED_PLUGINS` lists them.

**Plugin contract** (`specivo.core.plugin`):

```python
class PluginConfig(Protocol):
    name: str
    version: str
    def get_models(self) -> list: ...
    def get_routers(self, prefix: str) -> list: ...
    def get_services(self, registry: Any) -> None: ...
    def get_celery_tasks(self, celery_app: Any) -> None: ...
    def get_migration_path(self) -> Path | None: ...
    def get_template_dirs(self) -> list: ...
    def get_static_dirs(self) -> list[tuple[Path, str]]: ...
    def get_static_assets(self) -> dict[str, list[str]]: ...
    def get_locale_dirs(self) -> list[tuple[Path, str]]: ...
    def get_password_hashers(self) -> list: ...
    def on_startup(self, app: Any) -> None: ...
```

`BasePluginConfig(ABC)` provides no-op defaults for all hooks. Plugins are specified as dotted paths in `settings.installed_plugins` and loaded via `importlib.import_module`. `get_password_hashers` returns legacy password hashers (`WrappedLegacyHasher` subclasses from `specivo.services.password_hashers`); registering one does not enable it, since only algorithms listed in `LEGACY_PASSWORD_ALGORITHMS` ever verify a sign-in.

**No feature registry.** There is no flag mechanism: a page or route either exists or it does not. A plugin's optional behaviour checks for its own plugin or setting. A plugin class with extra attributes or hooks that core no longer calls keeps loading; nothing reads them.

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

**Commit before the response.** `DbSession` and `DB_SESSION` both declare `Depends(get_db, scope="function")`. With that scope FastAPI runs the code after `yield` -- the commit -- once the endpoint has returned and its response has been serialized, but *before* the response is sent. With FastAPI's default request scope the commit ran after the response had gone out, so a client that wrote and then read straight away (a browser `location.reload()`, an API client, an MCP agent) could read stale data. `tests/integration/test_commit_before_response.py` walks every route and fails on a session dependency without the function scope.

Consequences:

- The session is closed by the time a `StreamingResponse` body or a background task runs; those must open a session of their own from `get_session_factory()`.
- A yield dependency that takes the session (`get_reader`, `get_web_reader`) must itself be declared with `scope="function"`; FastAPI refuses to start otherwise.
- Explicit `await db.commit()` calls left in some endpoints from before this rule are harmless (the second commit is a no-op with `expire_on_commit=False`) but no longer needed.

MCP tool calls do not use `get_db`: `_get_session_and_user()` in `specivo.mcp.server` commits inside the tool function, before the tool result is returned.

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
- Per-task retry delays: email 60s, embedding 30s, link graph 15s. Outgoing webhook deliveries retry on their own schedule (`WEBHOOK_*` constants, see `specivo.services.webhook_delivery`).

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
- `serial` -- cannot run under pytest-xdist (shared state like Redis rate limits, MCP globals).
- `e2e` -- browser tests requiring a running server.
- `sso_idp` -- single sign-on tests against the on-demand identity provider (`make sso-idp-up`); skipped when it is not up.

**Client fixtures** in `specivo.testing.conftest_base`:
- `client` -- unauthenticated HTTPX `AsyncClient`.
- `auth_client` -- pre-authenticated as a regular user via JWT (user accessible via `auth_client.state.user`).
- `admin_client` -- pre-authenticated as an admin user via JWT.
- `agent_client` -- pre-authenticated as a service account via API key.
- `unauth_client` -- no DB override, for testing public endpoints.
- `db_session` -- direct async DB session for test data setup.

**Factories** are defined in `specivo.testing.factories` using `factory_boy`, and re-exported through `tests/factories/` shims that tests import from. Password hashing is done once at import time to avoid bcrypt cost per test:

```python
_TEST_PASSWORD_HASH = hash_password("testpassword")  # Computed once

class UserFactory(factory.Factory):
    class Meta:
        model = User
    login = factory.Sequence(lambda n: f"user{n}")
    password_hash = _TEST_PASSWORD_HASH
```

Variants: `AdminUserFactory`, `ServiceAccountFactory`.

**Test parallelism:** `addopts = "-n auto --dist loadfile"` in `pyproject.toml` -- tests run in parallel via pytest-xdist by default, one module per worker. `loadfile` rather than `worksteal` because modules insert rows on fixed unique keys, and splitting a module across workers deadlocks PostgreSQL when two transactions take those locks in opposite order (ADR-0002). Tests marked `serial` are excluded from parallel runs.

Operational detail — how to run the suite, the `DATABASE_URL` override the Makefile forces, fixtures, permission setup, and the gotchas — lives in the "Testing Conventions" wiki page in the SPECIVO project.

**Plugin isolation:** `_restore_plugin_manager` autouse fixture restores the global `PluginManager` singleton after each test. `pytest_collection_modifyitems` auto-skips `sso_idp` tests when the identity provider is not up.

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
- All events go to `security_audit_logs` (partitioned by month, with a default catch-all partition). The
  daily `ensure_audit_partitions` task (also run at worker start) creates months ahead and drops those older
  than `AUDIT_LOG_RETENTION_MONTHS` (default 12, `0` keeps everything; an environment setting on purpose).
- Every install records every event; there is no feature flag.
- An audit write never breaks the action it records: catch and log the failure.
- Code that cannot await (SSO events, session revocation) calls `record_event(...)`, which appends to the
  request's audit buffer (bound by `AuditBatchMiddleware`, flushed after the response). Outside a request,
  wrap the work in `async with audit_scope(session):`.
- Client-sent values are cut to their columns on every write path (`bounded_log` /
  `SecurityAuditService._persist`): `request_id` 36, `user_agent` 512, `ip_address` 45, and
  `details.query` 500 characters. A new write path goes through them, never `SecurityAuditLog(...)` directly.
- `log_event` fills `ip_address`, `request_id` and `user_agent` from `request`, or from the bound audit
  context, when not given.

**Scope.** Security and administrative events, and everything agents do through MCP. People's ordinary
edits to issues and wiki pages are not audit events: they live in issue and wiki history.

**Noise rules.** Each of these was one row per routine request, so it is not written:
- `access_granted` (the enum value stays so old rows read). `access_denied` is written, only where the
  caller passes `request`; anonymous paths pass none.
- `auth_failure` for a request without credentials, or for an expired token on the cookie path. A presented
  credential that is refused (a bad bearer token, an invalid, inactive or expired API key) is written, at
  most once a minute per client address and reason (Redis `SET NX EX 60`; written anyway when Redis is down).
  MCP refusals (a malformed token, or a key refused when a session opens or a tool runs) are written the same
  way with `source: mcp`, throttled apart from REST, in their own session because the tool's rolls back.
- A failed sign-in stores `login_hint` only as the login of the account the attempt named, otherwise
  `login_known: false`: a password typed into the login field must not become readable.
  A password reset request stores the address only when it matches an account, otherwise
  `email_known: false`, for the same reason.

**What `details` may hold:** ids, keys, slugs, names and titles of objects (file names, page titles, sprint
and schema names), enum values and codes, counts, flags, logins of accounts, group names, and search text
(capped at 500 characters). **Never:** tokens, codes, verifiers, secrets or any password material; issue,
comment, wiki or description body text; claim sets; request facts that have columns of their own
(`request_id`, `user_agent`). The admin viewer (`/admin/audit-log/`) shows `details` as escaped JSON, never
rendered as Markdown or HTML.

**Email change events.** `EMAIL_CHANGE_REQUESTED` (details: `new_email`, `address_in_use`, `resend`),
`EMAIL_CHANGE_FAILED` (`reason`, the error code) and `EMAIL_CHANGED` (`by` = `user` or `admin`,
`target_user_id`, `target_login`, `old_email`, `new_email`; the person's own change adds
`revoked_sessions`) are written by `EmailChangeService` (`specivo.services.email_change_service`), the
one path for the profile page, `/api/v1/auth/me/email/` and the admin routes. They are the only events
whose details hold email addresses, because the old and new address are the evidence of the change. A
confirmation link that matches no request is not audited.

**Currency events.** `CURRENCY_TOGGLED` (details: `code`, `is_active`) and `REPORTING_CURRENCY_CHANGED`
(`scope` = `instance` or `project`, `project_key` for a project, `old`, `new`; a project's `null` means it follows
the instance) are written by `CurrencyService` in the transaction of the change, only when the value changes.

**Exchange rate events.** `EXCHANGE_RATE_CREATED` / `_UPDATED` / `_DELETED` (details: `rate_id`, `rate_date`, `base`,
`quote`, `rate`, `source`; an update adds `changes`), `EXCHANGE_RATES_IMPORTED` (`rows`, `created`, `updated`,
`unchanged`, `date_from`, `date_to`), `EXCHANGE_RATES_FETCH_TOGGLED` (`enabled`, `url`) and
`EXCHANGE_RATE_SETTINGS_CHANGED` (`changes`: `{key: {old, new}}`) are written by `ExchangeRateService` in the
transaction of the change. Dry runs, refused imports and the scheduled fetch write none.

**AI model price events.** `AI_MODEL_PRICE_CREATED` / `_UPDATED` / `_DELETED` (details: `price_id`, `provider`,
`model`, `currency`, `effective_from`, `effective_to`, `source`, `is_active`, `prices`; a create that ended the open
period adds `closed_price_id`, an update adds `changes`) and `AI_MODEL_PRICES_SEEDED` (`as_of`, `inserted`, `closed`,
`history_inserted`, `aliases_updated`, `unchanged`, `skipped`; only when a load changed something, user none at
worker start) are written by `AiModelPriceService` in the transaction of the change (category Administration,
`resource_type` `ai_model_price`). The first seed in the migration writes none.

**AI subscription events.** `AI_SUBSCRIPTION_CREATED` / `_UPDATED` / `_DELETED` (details: `subscription_id`,
`target_user_id`, `target_login`, `provider`, `plan_key`, `plan_name`, `tool_slugs`, `amount` and `currency` at the
currency's minor units, `billing_period`, `starts_on`, `ends_on`; an update adds `changes`, where the notes appear
only as `{"changed": true}`) are written by `AiSubscriptionService` in the transaction of the change, only when the
actor is not the holder (an administrator changing somebody else's). `AI_SUBSCRIPTIONS_SELF_SERVICE_TOGGLED`
(`enabled`) is written when the setting changes. Category Administration; `resource_type` `ai_subscription` or
`setting`. Notes are free text and never logged.

**AI usage events** (category AI usage, `resource_type` `ai_usage_entry`, `project_id` set when the entry has one),
written by `AiUsageService` in the transaction of the change: `AI_USAGE_LOGGED_FOR_OTHER` (an entry created in another
user's name; details `entry_id`, `target_user_id`, `target_login`, `source`, `tool`, `provider`, `model`, `usage_date`,
`project_key`, `issue`, `reporter_api_key_id`; a replay that changes another user's bucket writes it again with
`status: "updated"` and `revision`, an unchanged replay none),
`AI_USAGE_UPDATED_BY_OTHER` (an edit by someone other than the entry's user; the same plus `changes`, comments only as
`{"changed": true}`), `AI_USAGE_EXCLUDED` / `AI_USAGE_INCLUDED` and `AI_USAGE_DELETED` (whoever did it; a delete adds
`tokens` and `cost_type`), and `AI_USAGE_COSTS_RECOMPUTED` (one per range: `provider`, `models`, `from`, `to`,
`entries`, `changed`, `before` and `after` totals per currency of `marginal` and `api_equivalent`; user none when the
task runs without an actor). Entries a person logs for themselves are not audited.

**Repository provider change.** `REPOSITORY_PROVIDER_CHANGED` (category Administration) is written by
`RepositoryIntegrationService.update` when an integration moves to another forge, which also moves its
hook URL and rotates its secret. Actor: whoever saved the change. `resource_type` is
`repository_integration` with the integration's id, and `project_id` is set. Details: `old_provider`,
`new_provider`, `old_repository`, `new_repository` (full names) and `secret_rotated: true`; never the
secret. Other edits of an integration are project settings, not audit events. Like every audit write,
a failure is logged and never fails the change (it runs in a savepoint).

**Development link detach and move.** `DEVELOPMENT_LINK_DETACHED` and `DEVELOPMENT_LINK_MOVED`
(category Issues and wiki) are written by `repository_activity.detach_item`, the one path for the
REST routes `DELETE /issues/{ref}/development/{commits|pull-requests}/{id}/` and `POST .../move/`.
Actor: whoever detached or moved the item. `resource_type` is `issue` with the source issue's id,
and `project_id` is its project. Details: `issue` (display key), `item_type` (`commit` or
`pull_request`), `item_id`, `item` (the SHA, or `#N` / `!N`), `repository` (full name); a move adds
`target_issue` and `target_project_id`. Never the commit message or the request's title. Like every
audit write, it runs in a savepoint and a failure is logged, never failing the change.

**Single sign-on events.** Each one is recorded by one function in `specivo.services.sso.events` (provider
configuration through `ProviderService._emit`), which also writes a structured `specivo.sso` log line
(`extra`: `event`, `request_id`, `audit`), so the trail also reaches the application log.

| Event | Actor (`user_id`) | Details |
|-------|-------------------|---------|
| `SSO_LOGIN_SUCCESS` | the user | `method`, `provider`, `iss`, `sub_hash` |
| `SSO_LOGIN_FAILURE` | none | `method`, `provider`, `iss`, `reason` (the `sso_*` error code) |
| `SSO_USER_PROVISIONED` | the new user | `provider`, `iss`, `sub_hash`, `user_id` |
| `SSO_ACCOUNT_LINKED` | the user (admin for an admin link) | `provider`, `iss`, `sub_hash`, `user_id`, `method` |
| `SSO_ACCOUNT_UNLINKED` | the user or the admin | `provider`, `iss`, `sub_hash`, `user_id`, `by`, `sessions_revoked` |
| `SSO_ROLES_SYNCED` | the user | `provider`, `sub_hash`, `admin_before`, `admin_after`, `groups_added`, `groups_removed` (user group ids) |
| `SSO_GROUPS_OVERAGE` | the user, if known | `provider`, `sub_hash`, `user_id`, `denied` |
| `SSO_LOGOUT` | the user | `provider`, `iss`, `sub_hash`, `user_id`, `idp_redirect` |
| `SSO_BACKCHANNEL_LOGOUT` | none (the IdP) | `provider`, `iss`, `sub_hash`, `by` (`sid`/`sub`), `sessions_revoked` |
| `SSO_PROVIDER_CREATED` / `_UPDATED` / `_DELETED` / `_SECRET_CHANGED` | the admin | `provider`, `iss`, `provider_id`, `fields` (names only); a delete adds its counts |
| `USER_DEPROVISIONED` | the admin, or none | `provider`, `user_id`, `reason`, `sessions_revoked` |
| `SESSIONS_REVOKED` | `actor_id` passed to `SessionService` | `reason`, `user_ids`, `session_count`, `row_count` |

`SSO_LOGIN_SUCCESS` / `SSO_LOGIN_FAILURE` replace the generic login events for SSO. `sub_hash` is the first 16 hex characters of SHA-256 of the subject. Details
never hold tokens, codes, verifiers, secrets, claim sets or group names.

## Consequences

**Positive:**
- Consistent patterns across all modules reduce cognitive load -- once you have seen one router/service/model, you know the structure of all of them.
- The `AppError` hierarchy with uniform JSON envelope means clients need a single error-handling path.
- Transaction-rollback test isolation keeps the test suite fast even as it grows.
- The plugin system lets an install add hashers, routes and templates without modifying the core.
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
