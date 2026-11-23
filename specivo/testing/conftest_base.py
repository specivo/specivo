"""Shared test fixtures for Specivo and its plugins.

Provides the core test infrastructure: engine, connection, session,
and HTTP client fixtures using transaction-rollback isolation.

Usage in tests/conftest.py::

    from specivo.testing.conftest_base import *  # noqa: F401, F403
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager, contextmanager

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

# Test env — ensure critical vars are set even if .env isn't loaded.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://specivo:specivo@localhost:5433/specivo_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")

# Each xdist worker gets its own Redis database. Redis is flushed after every
# test, and on a shared database one worker's flush would wipe another worker's
# rate-limit counters and JWT blocklist entries in the middle of a test.
# Database 0 stays with single-process runs; workers share 1-15 round robin.
_xdist_worker = os.environ.get("PYTEST_XDIST_WORKER", "")
if _xdist_worker.startswith("gw") and _xdist_worker[2:].isdigit():
    from urllib.parse import urlsplit, urlunsplit

    _redis_db = 1 + int(_xdist_worker[2:]) % 15
    os.environ["REDIS_URL"] = urlunsplit(urlsplit(os.environ["REDIS_URL"])._replace(path=f"/{_redis_db}"))
os.environ.setdefault("SECRET_KEY", "dev-secret-key-minimum-32-bytes-for-hs256-signing")
os.environ["DEBUG"] = "false"  # Always disable debug in tests (Makefile exports .env)
# Test users sign in through the real login endpoint on every client fixture; bcrypt at
# the production cost (12) is ~0.2 s per sign-in, which dominated the runtime of the suite.
# 4 is bcrypt's minimum. Tests that care about the cost set bcrypt_rounds themselves.
os.environ.setdefault("BCRYPT_ROUNDS", "4")
os.environ.setdefault("KILL_TOKEN", "test-kill-token-secret")

# Default to core-only mode (no plugins). Plugin repos set INSTALLED_PLUGINS
# in their own conftest.py *before* importing this module.
os.environ.setdefault("INSTALLED_PLUGINS", "[]")

from specivo.core.config import get_settings  # noqa: E402
from specivo.core.database import get_db  # noqa: E402
from specivo.main import create_app  # noqa: E402

# Clear lru_cache so get_settings() picks up our env vars above
get_settings.cache_clear()

TEST_DB_URL = get_settings().database_url


@asynccontextmanager
async def _test_lifespan(app: FastAPI):  # noqa: ARG001
    """No-op lifespan — skip eager DB/Redis checks in tests."""
    yield


def _create_test_app() -> FastAPI:
    """Create a FastAPI app with no-op lifespan for testing."""
    test_app = create_app()
    test_app.router.lifespan_context = _test_lifespan
    return test_app


# Single test app instance — lifespan won't eagerly connect to DB/Redis.
_test_app = _create_test_app()


# ---------------------------------------------------------------------------
# Engine (per-test to avoid event-loop issues)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Per-test async engine on pytest's event loop."""
    from specivo.core.database import _register_ltree_codec

    engine = create_async_engine(TEST_DB_URL, poolclass=NullPool)

    @sa_event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, connection_record):  # noqa: ARG001
        dbapi_connection.run_async(_register_ltree_codec)

    yield engine
    await engine.dispose()


@contextmanager
def global_engine_on(engine: AsyncEngine) -> Iterator[None]:
    """Point the process-global database engine at ``engine`` for the block.

    Code that opens its own sessions (the MCP tool calls, API key resolution,
    anything reached through ``create_app()``) uses the global engine from
    ``specivo.core.database``. Left alone, that engine is created by whichever
    test touches it first and stays bound to that test's event loop, so the
    next test fails with "Event loop is closed" or "attached to a different
    loop". The previous engine is restored on exit, and the Redis-backed MCP
    session store is dropped on both sides so it never holds a client from
    another loop.
    """
    from specivo.core import database
    from specivo.mcp.session_store import reset_mcp_session_store

    saved = database._engine
    database.set_engine(engine)
    reset_mcp_session_store()
    try:
        yield
    finally:
        reset_mcp_session_store()
        database._engine = saved
        database._session_factory = None


@pytest.fixture
def tool_engine(db_engine: AsyncEngine) -> Iterator[AsyncEngine]:
    """Run code that opens its own DB sessions on this test's engine and loop.

    Use it (``pytest.mark.usefixtures("tool_engine")``) in any test that goes
    through ``create_app()``, the MCP ``Client`` or the MCP HTTP transport, or
    that calls a helper opening its own session. See ``global_engine_on``.
    """
    with global_engine_on(db_engine):
        yield db_engine


# ---------------------------------------------------------------------------
# Connection + transaction (the core of rollback isolation)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _test_connection(db_engine: AsyncEngine) -> AsyncGenerator[AsyncConnection, None]:
    """Per-test connection with a transaction that will be rolled back."""
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        yield conn
        await transaction.rollback()


# ---------------------------------------------------------------------------
# Session (for direct DB access in tests)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine, _test_connection: AsyncConnection) -> AsyncGenerator[AsyncSession, None]:
    """Per-test session bound to the rollback connection.

    ``session.commit()`` is intercepted — it actually creates a savepoint
    so the outer transaction stays open. This means test code can call
    ``commit()`` freely (and data is visible within the same connection)
    but everything is rolled back after the test.
    """
    # Start a nested transaction (savepoint) so commit() restarts it
    # instead of committing the outer transaction.
    await _test_connection.begin_nested()

    session = AsyncSession(bind=_test_connection, expire_on_commit=False)

    # When the session commits (i.e. the savepoint is released),
    # immediately start a new savepoint so the outer txn stays open.
    @sa_event.listens_for(session.sync_session, "after_transaction_end")
    def _restart_savepoint(session_sync, transaction):
        if transaction.nested and not transaction._parent.nested:
            session_sync.begin_nested()

    yield session
    await session.close()

    # Clean up security audit logs committed via separate sessions
    # (error-path audit logs use independent connections for persistence,
    # bypassing the test's rollback-based isolation).
    try:
        from sqlalchemy import text as _text

        async with db_engine.begin() as _cleanup_conn:
            await _cleanup_conn.execute(_text("DELETE FROM security_audit_logs"))
    except Exception:  # noqa: BLE001, S110
        pass

    # Flush Redis state between tests
    try:
        import specivo.core.redis as _redis_module

        _redis_module._redis = None

        import redis.asyncio as aioredis

        r = aioredis.from_url(os.environ["REDIS_URL"])
        await r.flushdb()
        await r.aclose()
    except Exception:  # noqa: BLE001, S110
        pass


# ---------------------------------------------------------------------------
# HTTP clients
# ---------------------------------------------------------------------------


def _make_test_get_db(connection: AsyncConnection):
    """Create a get_db override that uses the test connection.

    Each call yields a NEW session on the same connection (same transaction).
    This ensures the app sees all data written by the test fixtures.
    """

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        await connection.begin_nested()
        session = AsyncSession(bind=connection, expire_on_commit=False)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    return _override


@pytest_asyncio.fixture
async def client(
    db_engine: AsyncEngine,
    _test_connection: AsyncConnection,
    db_session: AsyncSession,  # noqa: ARG001
) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client (unauthenticated).

    Injects the test connection so get_db() returns sessions on the
    same transaction as db_session.
    """
    from specivo.core.database import set_engine

    set_engine(db_engine)
    _test_app.dependency_overrides[get_db] = _make_test_get_db(_test_connection)

    transport = ASGITransport(app=_test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    _test_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def unauth_client() -> AsyncGenerator[AsyncClient, None]:
    """HTTP client with no DB override — for testing public endpoints."""
    transport = ASGITransport(app=_test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Authenticated client fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def auth_client(
    db_engine: AsyncEngine,  # noqa: ARG001
    _test_connection: AsyncConnection,
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    """Client pre-authenticated as a regular active user via JWT.

    Creates a user, logs in via the API, and attaches the resulting
    ``Authorization: Bearer <token>`` header to all subsequent requests.

    The fixture also exposes the logged-in user via ``auth_client.state.user``.
    """
    from specivo.testing.factories.user import TEST_PASSWORD, UserFactory

    _test_app.dependency_overrides[get_db] = _make_test_get_db(_test_connection)

    transport = ASGITransport(app=_test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user = UserFactory.build(login="auth_fixture_user", status="active")
        db_session.add(user)
        await db_session.flush()

        resp = await ac.post(
            "/api/v1/auth/login/",
            json={"login": user.login, "password": TEST_PASSWORD},
        )
        assert resp.status_code == 200, f"auth_client login failed: {resp.text}"  # noqa: S101
        token = resp.json()["access_token"]

        ac.headers["Authorization"] = f"Bearer {token}"
        ac.state = type("_State", (), {"user": user, "token": token})()  # type: ignore[attr-defined]

        yield ac

    _test_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin_client(
    db_engine: AsyncEngine,  # noqa: ARG001
    _test_connection: AsyncConnection,
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    """Client pre-authenticated as an admin user via JWT."""
    from specivo.testing.factories.user import TEST_PASSWORD, AdminUserFactory

    _test_app.dependency_overrides[get_db] = _make_test_get_db(_test_connection)

    transport = ASGITransport(app=_test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user = AdminUserFactory.build(login="admin_fixture_user", status="active")
        db_session.add(user)
        await db_session.flush()

        resp = await ac.post(
            "/api/v1/auth/login/",
            json={"login": user.login, "password": TEST_PASSWORD},
        )
        assert resp.status_code == 200, f"admin_client login failed: {resp.text}"  # noqa: S101
        token = resp.json()["access_token"]

        ac.headers["Authorization"] = f"Bearer {token}"
        ac.state = type("_State", (), {"user": user, "token": token})()  # type: ignore[attr-defined]

        yield ac

    _test_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def agent_client(
    db_engine: AsyncEngine,  # noqa: ARG001
    _test_connection: AsyncConnection,
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    """Client pre-authenticated as a service account via API key."""
    from specivo.services.api_key_service import ApiKeyService
    from specivo.testing.factories.user import ServiceAccountFactory

    _test_app.dependency_overrides[get_db] = _make_test_get_db(_test_connection)

    transport = ASGITransport(app=_test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user = ServiceAccountFactory.build(login="agent_fixture_user", status="active")
        db_session.add(user)
        await db_session.flush()

        service = ApiKeyService()
        _key, raw_key = await service.create_key(
            session=db_session,
            user_id=user.id,
            name="fixture-agent-key",
        )
        await db_session.flush()

        ac.headers["Authorization"] = f"Bearer {raw_key}"
        ac.state = type("_State", (), {"user": user, "raw_key": raw_key})()  # type: ignore[attr-defined]

        yield ac

    _test_app.dependency_overrides.clear()
