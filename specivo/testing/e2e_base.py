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

# ---------------------------------------------------------------------------
# Environment — same defaults as conftest_base.py
# ---------------------------------------------------------------------------

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://specivo:specivo@localhost:5433/specivo_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")
os.environ.setdefault("SECRET_KEY", "dev-secret-key-minimum-32-bytes-for-hs256-signing")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("KILL_TOKEN", "test-kill-token-secret")
os.environ.setdefault("INSTALLED_PLUGINS", "[]")
# DEBUG=true disables Secure flag on cookies so http://127.0.0.1 works.
# SQL_ECHO=false prevents SQLAlchemy from logging every query.
os.environ["DEBUG"] = "true"
os.environ["SQL_ECHO"] = "false"

E2E_SERVER_HOST = "127.0.0.1"
E2E_SERVER_PORT = int(os.environ.get("E2E_SERVER_PORT", "9944"))
E2E_BASE_URL = f"http://{E2E_SERVER_HOST}:{E2E_SERVER_PORT}"
E2E_PASSWORD = "testpassword"


# ---------------------------------------------------------------------------
# Session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _run_migrations():
    """Run alembic upgrade head once before E2E tests start."""
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
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
        ["uv", "run", "python", "-m", "specivo.cli.seed"],
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


@pytest.fixture(scope="session")
def e2e_server(_seed_lookups, _flush_redis) -> Generator[str, None, None]:
    """Start uvicorn, wait for /health/, yield base URL, stop on teardown."""
    # Kill any leftover server on our port
    subprocess.run(f"lsof -ti:{E2E_SERVER_PORT} | xargs kill -9", shell=True, capture_output=True)
    time.sleep(0.5)

    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "uvicorn",
            "specivo.main:app",
            "--host",
            E2E_SERVER_HOST,
            "--port",
            str(E2E_SERVER_PORT),
            "--log-level",
            "warning",
        ],
        env={**os.environ},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{E2E_BASE_URL}/health/", timeout=3)
            if r.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError, OSError):
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail(f"E2E server did not start within 20s on port {E2E_SERVER_PORT}.")

    yield E2E_BASE_URL

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def e2e_seed_data(e2e_server) -> dict:
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
""".format(db_url=os.environ["DATABASE_URL"], password=E2E_PASSWORD)

    result = subprocess.run(
        ["uv", "run", "python", "-c", script],
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
    assert resp.status_code == 200, f"E2E login failed for {login}: {resp.text}"

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


def create_project(api: httpx.Client, prefix: str = "E2E", **kwargs: object) -> dict:
    """Create a project via API with retry on 404 (parent race condition)."""
    key = unique_key(prefix)
    payload: dict = {"name": f"Test {key}", "identifier": key.lower(), "key": key, **kwargs}
    resp = api_post_with_retry(api, "/api/v1/projects/", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def create_issue(api: httpx.Client, project_key: str, subject: str, **kwargs: object) -> dict:
    """Create an issue via API with automatic tracker lookup and retry."""
    tracker_id = get_first_tracker_id(api)
    payload: dict = {"subject": subject, "project_key": project_key, "tracker_id": tracker_id, **kwargs}
    resp = api_post_with_retry(api, f"/api/v1/projects/{project_key}/issues/", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


_cached_tracker_id: int | None = None


def get_first_tracker_id(api: httpx.Client) -> int:
    """Get the first available tracker ID from the DB (cached per session)."""
    global _cached_tracker_id  # noqa: PLW0603
    if _cached_tracker_id is not None:
        return _cached_tracker_id
    # Query DB directly via psycopg2 (sync) to avoid event loop issues
    import subprocess

    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    result = subprocess.run(
        [
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
""",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        _cached_tracker_id = int(result.stdout.strip())
        return _cached_tracker_id
    raise RuntimeError(f"Could not find any trackers: {result.stderr}")
