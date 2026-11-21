"""Shared E2E (Playwright) test fixtures for Specivo and its plugins.

Provides session-scoped server startup, database seeding, and
per-test authenticated browser contexts.

Usage in tests/e2e/conftest.py::

    from specivo.testing.e2e_base import *  # noqa: F401, F403
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Generator
from uuid import uuid4

import httpx
import pytest
from playwright.sync_api import BrowserContext, Page

from specivo.web.assets import missing_asset_manifests

# ---------------------------------------------------------------------------
# Environment — same defaults as conftest_base.py
# ---------------------------------------------------------------------------

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://specivo:specivo@localhost:5433/specivo_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")
os.environ.setdefault("SECRET_KEY", "dev-secret-key-minimum-32-bytes-for-hs256-signing")
os.environ.setdefault("KILL_TOKEN", "test-kill-token-secret")
os.environ.setdefault("INSTALLED_PLUGINS", "[]")
# SQL_ECHO=false prevents SQLAlchemy from logging every query.
os.environ["SQL_ECHO"] = "false"

E2E_SERVER_HOST = "127.0.0.1"
E2E_SERVER_PORT = int(os.environ.get("E2E_SERVER_PORT", "9944"))
E2E_BASE_URL = f"http://{E2E_SERVER_HOST}:{E2E_SERVER_PORT}"
# The interface uvicorn listens on. The browser and the fixtures always use
# E2E_SERVER_HOST; only the SSO back-channel test needs more: the disposable
# Keycloak calls the server from a container, which on Linux cannot reach the
# host's loopback (``make test-e2e-sso`` listens on 0.0.0.0 there).
E2E_SERVER_BIND = os.environ.get("E2E_SERVER_BIND", E2E_SERVER_HOST)
# The public URL the SSO redirect URIs are built from. The disposable Keycloak
# realm (tests/fixtures/sso) registers http://localhost:9944, not 127.0.0.1.
E2E_SSO_BASE_URL = f"http://localhost:{E2E_SERVER_PORT}"

# Settings for the E2E server process only: pytest imports this module while
# collecting every run, and setting them in os.environ here would leak into
# any in-process test that rebuilds the settings after a cache_clear().
#
# DEBUG=true disables the Secure flag on cookies so http://127.0.0.1 works.
# RATE_LIMIT_ENABLED=false (unless set for the run) lets the browser tests sign
# in as often as they need; leaked into os.environ it would also switch the
# limiter off for the in-process rate-limit tests.
# APP_URL and SSO_ALLOW_INSECURE_HTTP let the SSO tests (``sso_idp``) sign in
# through the http-only disposable IdPs (Keycloak, authentik); while no SSO
# provider is configured they change nothing for the other tests.
_E2E_SERVER_ENV = {
    "DEBUG": "true",
    "RATE_LIMIT_ENABLED": os.environ.get("RATE_LIMIT_ENABLED", "false"),
    "APP_URL": E2E_SSO_BASE_URL,
    "SSO_ALLOW_INSECURE_HTTP": "true",
    # Check SSO sessions at the IdP on every renewal, so a browser test can
    # see a disabled user lose the session without waiting for the interval.
    "SSO_IDP_RECHECK_MINUTES": "0",
}
E2E_PASSWORD = "testpassword"  # noqa: S105

# A second server for the tests of an instance behind STEALTH_PREFIX: the whole
# UI, its assets and the API live under the prefix. Its own port, so the rest
# of the suite keeps the unprefixed server.
E2E_STEALTH_PREFIX = "/hidden-ui"
E2E_PREFIXED_SERVER_PORT = int(os.environ.get("E2E_PREFIXED_SERVER_PORT", str(E2E_SERVER_PORT + 1)))
E2E_PREFIXED_ORIGIN = f"http://{E2E_SERVER_HOST}:{E2E_PREFIXED_SERVER_PORT}"


# ---------------------------------------------------------------------------
# Session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _run_migrations():
    """Run alembic upgrade head once before E2E tests start."""
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],  # noqa: S607
        env={**os.environ},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"Alembic migration failed:\n{result.stderr}")


@pytest.fixture(scope="session")
def _seed_lookups(_run_migrations):
    """Seed lookup tables (trackers, statuses, priorities) via the CLI seed command."""
    result = subprocess.run(
        ["uv", "run", "python", "-m", "specivo.cli.seed"],  # noqa: S607
        env={**os.environ},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"Seed command failed:\n{result.stderr}")


@pytest.fixture(scope="session")
def e2e_base_url() -> str:
    """Base URL for the E2E test server."""
    return E2E_BASE_URL


@pytest.fixture(scope="session")
def _flush_redis():
    """Flush Redis to clear rate limit state from previous test runs."""
    import redis

    r = redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6380/0"))
    r.flushdb()
    r.close()


def kill_port_listeners(port: int) -> None:
    """Kill a leftover server LISTENING on *port*, and nothing else.

    A bare ``lsof -ti:<port>`` also lists every process with a connection to
    the port. Docker Desktop's backend proxies container traffic to
    ``host.docker.internal:<port>`` (an IdP calling the back-channel logout
    URL), so killing every listed process took the Docker engine down.
    """
    result = subprocess.run(  # noqa: S603
        ["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],  # noqa: S607
        capture_output=True,
        text=True,
    )
    for pid in result.stdout.split():
        if pid.isdigit():
            subprocess.run(["kill", "-9", pid], capture_output=True)  # noqa: S603, S607


@pytest.fixture(scope="session")
def e2e_server(_seed_lookups, _flush_redis) -> Generator[str, None, None]:
    """Start uvicorn, wait for /health/, yield base URL, stop on teardown."""
    _fail_without_bundles()
    proc = _start_server(E2E_SERVER_BIND, E2E_SERVER_PORT, {}, f"{E2E_BASE_URL}/health/")
    yield E2E_BASE_URL
    _stop_server(proc)


@pytest.fixture(scope="session")
def e2e_prefixed_server(_seed_lookups, _flush_redis) -> Generator[str, None, None]:
    """A server with ``STEALTH_PREFIX`` set; yields its base URL, prefix included.

    ``page.goto("/x/")`` resolves a leading slash against the origin, not the
    base URL's path, so the tests write ``f"{base}/x/"``.
    """
    _fail_without_bundles()
    base = f"{E2E_PREFIXED_ORIGIN}{E2E_STEALTH_PREFIX}"
    proc = _start_server(
        E2E_SERVER_HOST,
        E2E_PREFIXED_SERVER_PORT,
        {"STEALTH_PREFIX": E2E_STEALTH_PREFIX, "APP_URL": E2E_PREFIXED_ORIGIN},
        f"{base}/health/",
    )
    yield base
    _stop_server(proc)


def _fail_without_bundles() -> None:
    # The esbuild bundles are generated, not committed. Without them every page
    # loads with no Specivo CSS/JS, so fail once with the fix instead of per test.
    missing = missing_asset_manifests()
    if missing:
        pytest.fail(
            f"Frontend bundles not built (no manifest for: {', '.join(missing)}). "
            "Run `make frontend-build` before the E2E suite.",
            pytrace=False,
        )


def _start_server(bind: str, port: int, env: dict[str, str], health_url: str) -> subprocess.Popen:
    """Start uvicorn on *port* with the E2E settings and *env*; wait until *health_url* answers."""
    kill_port_listeners(port)
    time.sleep(0.5)

    proc = subprocess.Popen(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "uvicorn",
            "specivo.main:app",
            "--host",
            bind,
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env={**os.environ, **_E2E_SERVER_ENV, **env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            r = httpx.get(health_url, timeout=3)
            if r.status_code == 200:
                return proc
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError, OSError):
            pass
        time.sleep(0.5)
    proc.kill()
    pytest.fail(f"E2E server did not start within 20s on port {port}.")


def _stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def e2e_seed_data(e2e_server) -> dict:  # noqa: ARG001
    """Create test users via a subprocess. Returns credentials dict.

    Uses a subprocess with psycopg2-style INSERT to avoid asyncio
    event loop conflicts and handle the LOWER() functional unique index.
    """
    script = """
import asyncio, os, sys
sys.path.insert(0, ".")
os.environ.setdefault("DATABASE_URL", "{db_url}")
os.environ.setdefault("SECRET_KEY", "dev-secret-key-minimum-32-bytes-for-hs256-signing")

async def _seed():
    import asyncpg
    from specivo.services.auth_utils import hash_password
    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(db_url)
    try:
        pw_hash = hash_password("{password}")
        # Check if users already exist (functional unique index on LOWER(login))
        for login, email, display, is_admin in [
            ("e2e_user", "e2e@test.local", "E2E User", False),
            ("e2e_admin", "e2e_admin@test.local", "E2E Admin", True),
        ]:
            existing = await conn.fetchval(
                "SELECT id FROM users WHERE LOWER(login) = LOWER($1)", login
            )
            if not existing:
                await conn.execute(
                    "INSERT INTO users (login, email, password_hash, display_name, status, is_admin) "
                    "VALUES ($1, $2, $3, $4, 'active', $5)",
                    login, email, pw_hash, display, is_admin,
                )
    finally:
        await conn.close()

asyncio.run(_seed())
""".format(db_url=os.environ["DATABASE_URL"], password=E2E_PASSWORD)  # noqa: S608

    result = subprocess.run(  # noqa: S603
        ["uv", "run", "python", "-c", script],  # noqa: S607
        env={**os.environ},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"E2E seed data failed:\n{result.stderr}")

    return {
        "user_login": "e2e_user",
        "admin_login": "e2e_admin",
        "password": E2E_PASSWORD,
    }


# ---------------------------------------------------------------------------
# Browser context helpers
# ---------------------------------------------------------------------------


def _login_and_get_token(base_url: str, login: str, password: str) -> tuple[str, list[dict]]:
    """Login via API. Returns (access_token, browser_cookies)."""
    resp = httpx.post(
        f"{base_url}/api/v1/auth/login/",
        json={"login": login, "password": password},
        timeout=10,
    )
    assert resp.status_code == 200, f"E2E login failed for {login}: {resp.text}"  # noqa: S101

    token = resp.json()["access_token"]
    cookies = []
    for name, value in resp.cookies.items():
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": E2E_SERVER_HOST,
                "path": "/",
                "httpOnly": True,
                "secure": False,
                "sameSite": "Lax",
            }
        )
    return token, cookies


# ---------------------------------------------------------------------------
# Session-scoped tokens (login once, reuse across all tests)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _user_auth(e2e_server, e2e_seed_data) -> tuple[str, list[dict]]:
    """Login as regular user once per session."""
    return _login_and_get_token(e2e_server, e2e_seed_data["user_login"], e2e_seed_data["password"])


@pytest.fixture(scope="session")
def _admin_auth(e2e_server, e2e_seed_data) -> tuple[str, list[dict]]:
    """Login as admin once per session."""
    return _login_and_get_token(e2e_server, e2e_seed_data["admin_login"], e2e_seed_data["password"])


# ---------------------------------------------------------------------------
# Per-test authenticated contexts
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_context(browser, e2e_server, _user_auth) -> Generator[BrowserContext, None, None]:
    """Browser context with auth cookies for a regular user."""
    _, cookies = _user_auth
    context = browser.new_context(base_url=e2e_server)
    context.add_cookies(cookies)
    yield context
    context.close()


@pytest.fixture
def auth_page(auth_context) -> Generator[Page, None, None]:
    """Page within an authenticated browser context (regular user)."""
    page = auth_context.new_page()
    yield page
    page.close()


@pytest.fixture
def admin_context(browser, e2e_server, _admin_auth) -> Generator[BrowserContext, None, None]:
    """Browser context with auth cookies for an admin user."""
    _, cookies = _admin_auth
    context = browser.new_context(base_url=e2e_server)
    context.add_cookies(cookies)
    yield context
    context.close()


@pytest.fixture
def admin_page(admin_context) -> Generator[Page, None, None]:
    """Page within an authenticated browser context (admin user)."""
    page = admin_context.new_page()
    yield page
    page.close()


# ---------------------------------------------------------------------------
# API client for test data seeding
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client(e2e_server, _admin_auth) -> Generator[httpx.Client, None, None]:
    """httpx Client authenticated as admin, for seeding test data via REST API."""
    token, _ = _admin_auth
    client = httpx.Client(base_url=e2e_server, timeout=10)
    client.headers["Authorization"] = f"Bearer {token}"
    yield client
    client.close()


# ---------------------------------------------------------------------------
# The instance behind STEALTH_PREFIX (e2e_prefixed_server)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _prefixed_admin_auth(e2e_prefixed_server, e2e_seed_data) -> tuple[str, list[dict]]:
    """Login as admin once per session, on the prefixed server."""
    return _login_and_get_token(e2e_prefixed_server, e2e_seed_data["admin_login"], e2e_seed_data["password"])


@pytest.fixture
def prefixed_admin_page(browser, _prefixed_admin_auth) -> Generator[Page, None, None]:
    """A page signed in as admin on the prefixed server (``goto(f"{base}/...")``)."""
    _, cookies = _prefixed_admin_auth
    context = browser.new_context()
    context.add_cookies(cookies)
    page = context.new_page()
    yield page
    context.close()


@pytest.fixture
def prefixed_api(e2e_prefixed_server, _prefixed_admin_auth) -> Generator[httpx.Client, None, None]:
    """httpx Client authenticated as admin on the prefixed server; paths are under the prefix."""
    token, _ = _prefixed_admin_auth
    client = httpx.Client(base_url=e2e_prefixed_server, timeout=10)
    client.headers["Authorization"] = f"Bearer {token}"
    yield client
    client.close()


def unique_key(prefix: str = "E2E") -> str:
    """Generate a unique project key for test isolation."""
    return f"{prefix}{uuid4().hex[:5].upper()}"


def api_post_with_retry(
    api: httpx.Client,
    url: str,
    *,
    json: dict | None = None,
    retries: int = 3,
    delay: float = 0.5,
) -> httpx.Response:
    """POST with retry on 404 — handles CI race conditions where a parent resource isn't visible yet."""
    resp = httpx.Response(status_code=0)
    for attempt in range(retries):
        resp = api.post(url, json=json)
        if resp.status_code != 404 or attempt == retries - 1:
            return resp
        time.sleep(delay)
    return resp


def wait_for_alpine(page: Page, timeout: float = 15000) -> None:
    """Block until Alpine has booted, before asserting on Alpine-rendered DOM.

    Markup produced by ``x-for`` or a ``:class`` binding does not exist until
    the bundle has downloaded, parsed and initialised. Playwright's default 5s
    assertion budget covers that on a developer machine but not reliably on a
    loaded CI runner, where the result is a "element(s) not found" failure that
    reads like a broken feature rather than a slow one.
    """
    page.wait_for_function("() => window.Alpine !== undefined", timeout=timeout)


def create_project(api: httpx.Client, prefix: str = "E2E", **kwargs: object) -> dict:
    """Create a project via API with retry on 404 (parent race condition)."""
    key = unique_key(prefix)
    payload: dict = {"name": f"Test {key}", "identifier": key.lower(), "key": key, **kwargs}
    resp = api_post_with_retry(api, "/api/v1/projects/", json=payload)
    assert resp.status_code == 201, resp.text  # noqa: S101
    created: dict = resp.json()
    return created


def create_issue(api: httpx.Client, project_key: str, subject: str, **kwargs: object) -> dict:
    """Create an issue via API with automatic tracker lookup and retry."""
    tracker_id = get_first_tracker_id(api)
    payload: dict = {"subject": subject, "project_key": project_key, "tracker_id": tracker_id, **kwargs}
    resp = api_post_with_retry(api, f"/api/v1/projects/{project_key}/issues/", json=payload)
    assert resp.status_code == 201, resp.text  # noqa: S101
    created: dict = resp.json()
    return created


_cached_tracker_id: int | None = None


def get_first_tracker_id(api: httpx.Client) -> int:  # noqa: ARG001
    """Get the first available tracker ID from the DB (cached per session)."""
    global _cached_tracker_id
    if _cached_tracker_id is not None:
        return _cached_tracker_id
    # Query DB directly via psycopg2 (sync) to avoid event loop issues
    import subprocess

    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "python",
            "-c",
            f"""
import asyncio, asyncpg
async def _f():
    conn = await asyncpg.connect("{db_url}")
    try:
        return await conn.fetchval("SELECT id FROM trackers ORDER BY position LIMIT 1")
    finally:
        await conn.close()
print(asyncio.run(_f()))
""",  # noqa: S608
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        _cached_tracker_id = int(result.stdout.strip())
        return _cached_tracker_id
    raise RuntimeError(f"Could not find any trackers: {result.stderr}")
