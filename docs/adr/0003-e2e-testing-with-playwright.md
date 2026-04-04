# ADR-0003: E2E Testing with Playwright

**Date:** 2026-04-04
**Status:** Accepted
**Deciders:** Boris

## Context

Specivo's backend integration tests (ADR-0002) verify API responses and rendered HTML via `httpx.AsyncClient`, but cannot test browser behavior: Alpine.js form submissions, HTMX partial swaps, cookie-based auth flows, sidebar navigation, or JavaScript-driven components. A browser-based test layer is needed to catch issues that only manifest in a real browser.

The frontend uses a zero-build-step stack: Jinja2 server-side rendering, Alpine.js for reactivity, HTMX for partial updates, Bootstrap 5 for layout.

## Decision

### Framework: pytest-playwright (Python)

Playwright via `pytest-playwright` — same pytest ecosystem as backend tests, no Node.js tooling. Chromium-only in CI (Firefox/WebKit can be added later).

**Why not Selenium:** Playwright has auto-waiting, better async support, trace recording on failure, and faster execution.

**Why not Cypress:** Requires Node.js, separate test runner, and doesn't integrate with pytest markers.

### Architecture: real server, not ASGI transport

Unlike backend tests that use `httpx.AsyncClient` with in-process ASGI transport, E2E tests start a real uvicorn server. This tests the full stack including middleware, static file serving, cookie handling, and CORS.

```
pytest → starts uvicorn subprocess (port 9944) → Chromium connects → tests run → uvicorn killed
```

### Fixture hierarchy

**Session-scoped (run once):**
- `_run_migrations` — alembic upgrade head
- `_seed_lookups` — seed trackers, statuses, priorities via CLI
- `_flush_redis` — clear rate limit state
- `e2e_server` — start uvicorn, wait for /health/, yield base URL
- `e2e_seed_data` — create e2e_user + e2e_admin via asyncpg subprocess
- `_user_auth` / `_admin_auth` — login once, cache token + cookies

**Function-scoped (per test):**
- `auth_context` / `admin_context` — browser context with injected cookies
- `auth_page` / `admin_page` — page within context
- `api_client` — httpx Client for seeding test data via REST API

### Auth strategy: cookie injection

Login happens once per session via `httpx.post("/api/v1/auth/login/")`. The returned `access_token` cookie is injected into each browser context via `context.add_cookies()`. This avoids rate limiting and saves ~500ms per test.

The login page itself is tested separately in `test_login.py` using the raw `page` fixture (no cookie injection).

### Data isolation

E2E tests cannot use transaction rollback (the server has its own connection pool). Instead:
- Each test creates data via `api_client` with unique identifiers (`unique_key()`)
- No cleanup between tests — data accumulates but keys are unique
- Test database uses tmpfs (`docker-compose.test.yml`) — destroyed on container stop

### Page Object Models

Locator logic is separated from assertions:

```
tests/e2e/pages/
    login_page.py
    dashboard_page.py
    issue_list_page.py
    issue_form_page.py
    wiki_page.py
    search_page.py
    admin_page.py
```

### Plugin extensibility

Shared fixtures live in `specivo/testing/e2e_base.py` (mirrors `conftest_base.py`). Plugin repos import them:

```python
# specivo-pro/tests/e2e/conftest.py
from specivo.testing.e2e_base import *  # noqa: F401, F403
```

Pro/enterprise tests use existing markers: `pytestmark = [pytest.mark.e2e, pytest.mark.pro]`

### CI integration

E2E tests run as a separate CI job after backend tests pass:

```yaml
# GitHub Actions / GitLab CI
- uv run playwright install chromium --with-deps
- uv run pytest tests/e2e/ -m e2e -n 0
```

Traces saved as artifacts on failure for debugging via Playwright Trace Viewer.

### What E2E tests cover (and what they don't)

**E2E tests (browser-required):**
- Alpine.js form behavior (login, issue create/edit)
- HTMX partial swaps (pagination, filters)
- Cookie-based auth flow (login redirect, logout)
- Sidebar navigation, responsive layout
- Full page rendering with JS-driven components

**Already covered by backend tests (skip in E2E):**
- API response schemas, status codes
- Rate limiting, auth token mechanics
- Search ranking accuracy
- Permission enforcement at API level
- Data validation edge cases

## Consequences

**Positive:**
- Catches JS-only bugs (Alpine.js binding, HTMX swaps, cookie auth)
- Same pytest ecosystem — markers, fixtures, CI pipelines
- 31 tests run in ~11 seconds headless
- Plugin repos extend naturally via shared fixtures
- `make test-e2e-headed` for visual debugging

**Negative:**
- Requires running database + Redis (same as integration tests)
- Requires `playwright install chromium` (one-time, ~130MB)
- Alpine.js `x-model` binding can be tricky with Playwright's `fill()` — some components need `press_sequentially()` or `dispatch_event("input")`
- Server stdout pipe must be DEVNULL to prevent blocking on SQL echo output

## Makefile targets

```makefile
make test-e2e          # Headless, chromium
make test-e2e-headed   # Visible browser + 300ms slowmo
make test-e2e-debug    # Playwright Inspector (PWDEBUG=1)
make playwright-install # Install chromium browser
```

## File locations

| Component | Path |
|-----------|------|
| Shared E2E fixtures | `specivo/testing/e2e_base.py` |
| Core E2E conftest | `tests/e2e/conftest.py` |
| Page Object Models | `tests/e2e/pages/` |
| Test files | `tests/e2e/test_*.py` |
