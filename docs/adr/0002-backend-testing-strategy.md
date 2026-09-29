# ADR-0002: Backend Testing Strategy

**Date:** 2026-04-04
**Revised:** 2026-09-08 — corrected the xdist distribution mode (`loadfile`, not `worksteal`),
recorded the `slow` and `redmine` markers, and noted that tests import factories through the
`tests/factories/` shims. The operational detail now lives in the "Testing Conventions" wiki page
in the SPECIVO project; this ADR keeps the decisions and their rationale.
**Status:** Accepted
**Deciders:** Boris

## Context

Specivo needs a fast, reliable backend test suite that covers API endpoints, service logic, and web page rendering without requiring a browser. The project has a plugin-based architecture (core / pro / enterprise) where tests must run in core-only mode and extend cleanly when plugins are installed.

Key constraints:
- Async codebase (FastAPI, SQLAlchemy 2.0 async, asyncpg)
- Tests must be fast enough to run on every commit (~1ms teardown per test)
- Plugin repos import shared fixtures from core
- CI runs on both GitLab CI and GitHub Actions

## Decision

### Test framework

pytest with pytest-asyncio (`asyncio_mode = "auto"`) and pytest-xdist for parallel execution.

### Isolation strategy: transaction rollback

Each test runs inside a top-level database transaction that is rolled back after the test completes. This replaces the common TRUNCATE approach.

```
test start → BEGIN → test runs (commits create savepoints) → ROLLBACK → test end
```

**Why not TRUNCATE:** ~1ms teardown vs ~300ms. At the suite's current size (~2,300 backend tests) that difference decides whether the suite is runnable on every commit.

**How it works:**
- `_test_connection` fixture opens a connection and begins a transaction
- `db_session` fixture creates a session bound to that connection
- SQLAlchemy event listener intercepts `session.commit()` and converts it to a savepoint
- On teardown, the outer transaction rolls back — all data vanishes
- Security audit logs (written via separate connections) are cleaned up explicitly
- Redis state is flushed between tests

### HTTP client fixtures

Tests use `httpx.AsyncClient` with `ASGITransport` — no real HTTP server needed. Four pre-built client fixtures:

| Fixture | Auth | Use case |
|---------|------|----------|
| `client` | None | Unauthenticated API tests with DB override |
| `unauth_client` | None | Public endpoint tests (no DB override) |
| `auth_client` | JWT (regular user) | Authenticated API and web page tests |
| `admin_client` | JWT (admin user) | Admin endpoint tests |
| `agent_client` | API key (service account) | Agent/automation endpoint tests |

All fixtures are defined in `specivo/testing/conftest_base.py` and re-exported in each repo's `tests/conftest.py`.

### Test markers and edition gating

```python
markers = [
    "unit",         # Pure logic, no database
    "integration",  # API endpoints, requires database
    "service",      # Service layer, requires database
    "slow",         # Over 5 seconds
    "serial",       # Cannot run under xdist (shared state)
    "pro",          # Requires specivo-pro plugin
    "enterprise",   # Requires specivo-enterprise plugin
    "e2e",          # Browser tests, excluded from backend runs
    "redmine",      # Importer tests against the on-demand Redmine fixture
]
```

`--strict-markers` is on, so an undeclared marker is a collection error rather than a silent no-op.

`pytest_collection_modifyitems` in `tests/conftest.py` skips what is not available: `@pytest.mark.pro` and `@pytest.mark.enterprise` when the plugin is not in `INSTALLED_PLUGINS`, and `@pytest.mark.redmine` when nothing is listening on the importer fixture's database port. The fixture is probed with a raw socket rather than a driver connection, so collection stays cheap when it is down.

### Test data

Factory classes build model instances with sensible defaults. Tests create their own data via factories + `db_session` — no shared seed data.

The definitions live in `specivo/testing/factories/` so plugin repos can share them; `tests/factories/` holds thin re-export shims, and tests import from **`tests.factories.*`**. Bcrypt hashing happens once at import time — `UserFactory` carries a pre-computed hash for the password exported as `TEST_PASSWORD`.

### Parallel execution

`addopts = "-n auto --dist loadfile"` runs tests in parallel via xdist. Tests marked `serial` (rate limiting, Redis-dependent, MCP global state) run in a separate pass with `-n 0`.

**`loadfile`, not `worksteal`.** Test modules define fixtures that insert rows on fixed unique keys — logins, project keys. Spreading one module's tests across workers lets two of them create those rows concurrently, and when the two transactions take the locks in opposite order PostgreSQL aborts one with a deadlock. Pinning a module to a single worker removes the concurrency; files still run in parallel with each other, at the same total runtime.

## Consequences

**Positive:**
- 700+ tests run in seconds with parallel execution
- Plugin repos share the same fixture infrastructure
- No test ordering dependencies — each test is self-contained
- Transaction rollback is invisible to application code (commit works normally)

**Negative:**
- Cannot test cross-connection visibility (e.g., NOTIFY/LISTEN)
- Security audit logs need explicit cleanup (they bypass the rollback connection)
- Redis must be flushed between tests

## File locations

| Component | Path |
|-----------|------|
| Shared fixtures | `specivo/testing/conftest_base.py` |
| Factories | `specivo/testing/factories/` |
| Core conftest | `tests/conftest.py` |
| Factory re-export shims | `tests/factories/` |
| Unit tests | `tests/unit/` |
| Integration tests | `tests/integration/` |
| Service tests | `tests/services/` |
| Operational reference | "Testing Conventions" wiki page, SPECIVO project |

There is no `tests/models/`; model and schema-contract tests go in `tests/integration/`, the general "needs a database" bucket.
