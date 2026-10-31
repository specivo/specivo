"""SCIM rate limits (Redis-backed, so serial): per provider, and per client address for failed authentication."""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from tests.integration.scim.conftest import ScimClient, make_scim_provider

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6380/0")


@pytest_asyncio.fixture(autouse=True)
async def clean_buckets(monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    r = aioredis.from_url(_REDIS_URL, decode_responses=True)

    async def _clear() -> None:
        keys = await r.keys("rl:scim*")
        if keys:
            await r.delete(*keys)

    await _clear()
    yield
    await _clear()
    await r.aclose()


async def test_per_provider_limit(client: AsyncClient, db_session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "scim_rate_limit_per_minute", 3)
    _a, token_a = await make_scim_provider(db_session, "scim-rl-a")
    _b, token_b = await make_scim_provider(db_session, "scim-rl-b")
    a = ScimClient(client, "scim-rl-a", token_a)
    b = ScimClient(client, "scim-rl-b", token_b)

    statuses = [(await a.get("/Users")).status_code for _ in range(4)]
    assert statuses == [200, 200, 200, 429]
    last = await a.get("/Users")
    assert last.json()["status"] == "429" and "Retry-After" in last.headers
    # Another provider has its own bucket.
    assert (await b.get("/Users")).status_code == 200


async def test_failed_authentication_is_limited_per_client(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "scim_unauthenticated_rate_limit_per_minute", 2)
    _p, token = await make_scim_provider(db_session, "scim-rl-c")
    bad = ScimClient(client, "scim-rl-c", "wrong-token")
    statuses = [(await bad.get("/Users")).status_code for _ in range(3)]
    assert statuses == [401, 401, 429]
    # The provider's own bucket is untouched: the real IdP keeps working.
    assert (await ScimClient(client, "scim-rl-c", token).get("/Users")).status_code == 200
