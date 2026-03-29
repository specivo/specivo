"""Integration tests for the login rate limit endpoint.

These tests exercise the full HTTP stack with a real Redis instance on
the Redis instance configured via ``REDIS_URL`` environment variable.

All tests are skipped gracefully if Redis is unavailable so CI without
Redis does not fail.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import AsyncClient

# Rate limit tests depend on exact Redis counter state. Other xdist workers
# also call /auth/login from the same IP (127.0.0.1), polluting the shared
# counter. These tests are skipped under xdist and run in serial CI instead.

# ---------------------------------------------------------------------------
# Skip marker — applied to the entire module if Redis is unavailable
# ---------------------------------------------------------------------------

_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6380/0")
_RATE_LIMIT_KEY_PREFIX = "rl:auth_login:"

# We'll set this at collection time
_redis_available: bool | None = None


def _check_redis_sync() -> bool:
    """Return True if Redis is reachable. Parses host:port from _REDIS_URL."""
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(_REDIS_URL)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    try:
        s = socket.create_connection((host, port), timeout=1)
        s.close()
        return True
    except OSError:
        return False


_redis_available = _check_redis_sync()


def _is_xdist() -> bool:
    """Detect if running under pytest-xdist with multiple workers."""
    return os.environ.get("PYTEST_XDIST_WORKER") is not None


pytestmark = [
    pytest.mark.serial,
    pytest.mark.skipif(
        not _redis_available,
        reason=f"Redis not available at {_REDIS_URL}",
    ),
    pytest.mark.skipif(
        _is_xdist(),
        reason="Rate limit tests require serial execution (shared Redis state)",
    ),
]


# ---------------------------------------------------------------------------
# Redis cleanup fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def flush_rate_limit_keys():
    """Remove all rate limit keys before each test for a clean slate."""
    if not _redis_available:
        yield
        return

    r = aioredis.from_url(_REDIS_URL, decode_responses=True)
    keys = await r.keys("rl:*")
    if keys:
        await r.delete(*keys)
    yield
    keys = await r.keys("rl:*")
    if keys:
        await r.delete(*keys)
    await r.aclose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_allows_requests_under_limit(client: AsyncClient):
    """Requests under the 10/min limit should all return non-429."""
    # We send 3 requests; all should succeed (even with bad credentials → 401,
    # not 429, because the rate limit passes them through).
    for _ in range(3):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"login": "nosuchuser", "password": "wrongpass"},
        )
        assert resp.status_code != 429, f"Unexpected 429 on request under limit: {resp.text}"


@pytest.mark.asyncio
async def test_login_returns_429_after_limit_exceeded(client: AsyncClient):
    """The 11th login attempt within 60 s must return 429."""
    payload = {"login": "nosuchuser", "password": "wrongpass"}
    for i in range(10):
        resp = await client.post("/api/v1/auth/login", json=payload)
        assert resp.status_code != 429, f"Request {i + 1} got unexpected 429"

    # 11th request — must be rate-limited
    resp = await client.post("/api/v1/auth/login", json=payload)
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_429_response_has_retry_after_header(client: AsyncClient):
    """429 response must include a Retry-After header with a positive value."""
    payload = {"login": "x", "password": "x"}
    for _ in range(10):
        await client.post("/api/v1/auth/login", json=payload)

    resp = await client.post("/api/v1/auth/login", json=payload)
    assert resp.status_code == 429

    retry_after = resp.headers.get("Retry-After")
    assert retry_after is not None, "Retry-After header missing from 429 response"
    assert int(retry_after) > 0


@pytest.mark.asyncio
async def test_x_ratelimit_remaining_decrements(client: AsyncClient):
    """X-RateLimit-Remaining header should decrement with each request."""
    payload = {"login": "x", "password": "x"}
    remainders = []

    for _ in range(5):
        resp = await client.post("/api/v1/auth/login", json=payload)
        if resp.status_code == 429:
            break
        header = resp.headers.get("X-RateLimit-Remaining")
        if header is not None:
            remainders.append(int(header))

    # Verify the sequence is strictly decreasing
    assert len(remainders) >= 2, "Need at least 2 non-429 responses to check decrement"
    for i in range(1, len(remainders)):
        assert remainders[i] < remainders[i - 1], f"X-RateLimit-Remaining did not decrement: {remainders}"


@pytest.mark.asyncio
async def test_429_response_body_has_error_code(client: AsyncClient):
    """429 body must follow the standard error envelope with code rate_limit_exceeded."""
    payload = {"login": "x", "password": "x"}
    for _ in range(10):
        await client.post("/api/v1/auth/login", json=payload)

    resp = await client.post("/api/v1/auth/login", json=payload)
    assert resp.status_code == 429

    body = resp.json()
    assert "errors" in body
    assert len(body["errors"]) == 1
    assert body["errors"][0]["code"] == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_x_ratelimit_limit_header_present(client: AsyncClient):
    """X-RateLimit-Limit header should be present and equal the configured limit."""
    resp = await client.post(
        "/api/v1/auth/login",
        json={"login": "x", "password": "x"},
    )
    # Even 401 responses (bad credentials) should carry the rate limit header
    assert resp.status_code != 429
    limit_header = resp.headers.get("X-RateLimit-Limit")
    assert limit_header is not None
    assert int(limit_header) == 10
