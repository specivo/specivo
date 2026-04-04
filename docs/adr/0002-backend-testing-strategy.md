# ADR-0002: Backend Testing Strategy

**Date:** 2026-04-04
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

**Why not TRUNCATE:** ~1ms teardown vs ~300ms. With 700+ tests, that difference is significant.

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
    "serial",       # Cannot run under xdist (shared state)
    "pro",          # Requires specivo-pro plugin
    "enterprise",   # Requires specivo-enterprise plugin
    "e2e",          # Browser tests, excluded from backend runs
]
```

`pytest_collection_modifyitems` in `tests/conftest.py` auto-skips `@pytest.mark.pro` and `@pytest.mark.enterprise` when the corresponding plugins are not in `INSTALLED_PLUGINS`.

### Test data

Factory classes (`specivo/testing/factories/`) build model instances with sensible defaults. Tests create their own data via factories + `db_session` — no shared seed data.

### Parallel execution

`addopts = "-n auto --dist worksteal"` runs tests in parallel via xdist. Tests marked `serial` (rate limiting, Redis-dependent) run in a separate pass with `-n 0`.

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
| Unit tests | `tests/unit/` |
| Integration tests | `tests/integration/` |
