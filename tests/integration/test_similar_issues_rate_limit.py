"""The similar-issues endpoint is rate limited per user, not per client address (Redis-backed, so serial)."""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from tests.integration.test_similar_issues_api import CREATE, World, grant, login, make_issue, similar
from tests.integration.test_similar_issues_api import world as world  # noqa: F401 (fixture)

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6380/0")


@pytest_asyncio.fixture(autouse=True)
async def clean_buckets(monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    r = aioredis.from_url(_REDIS_URL, decode_responses=True)

    async def _clear() -> None:
        keys = await r.keys("rl:*")
        if keys:
            await r.delete(*keys)

    await _clear()
    yield
    await _clear()
    await r.aclose()


async def test_sixty_first_request_in_a_minute_is_429(client: AsyncClient, db_session: AsyncSession, world: World):
    await grant(db_session, world.project, world.caller, CREATE)
    await make_issue(db_session, world)
    headers = await login(client, world.caller)

    statuses = [(await similar(client, headers)).status_code for _ in range(60)]
    assert statuses == [200] * 60

    resp = await similar(client, headers)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) > 0


async def test_limit_is_per_user_not_per_ip(client: AsyncClient, db_session: AsyncSession, world: World):
    await grant(db_session, world.project, world.caller, CREATE)
    await grant(db_session, world.project, world.other, CREATE)
    await make_issue(db_session, world)
    first = await login(client, world.caller)
    second = await login(client, world.other)

    for _ in range(60):
        await similar(client, first)
    assert (await similar(client, first)).status_code == 429

    assert (await similar(client, second)).status_code == 200
