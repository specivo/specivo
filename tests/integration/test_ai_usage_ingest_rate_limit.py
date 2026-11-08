"""The AI usage ingest rate limit: per account, 429 with Retry-After (Redis-backed, so serial)."""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.services.api_key_service import ApiKeyService
from tests.factories.user import ServiceAccountFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6380/0")
URL = "/api/v1/ai-usage/ingest"


@pytest_asyncio.fixture(autouse=True)
async def clean_buckets(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_usage_ingest_rate_limit_per_minute", 2)
    r = aioredis.from_url(_REDIS_URL, decode_responses=True)

    async def _clear() -> None:
        keys = await r.keys("rl:ai_usage_ingest*")
        if keys:
            await r.delete(*keys)

    await _clear()
    yield
    await _clear()
    await r.aclose()


async def _key(db: AsyncSession, login: str) -> dict[str, str]:
    user = ServiceAccountFactory.build(login=login, status="active")
    db.add(user)
    await db.flush()
    _key, raw = await ApiKeyService().create_key(db, user_id=user.id, name="rl", check_scope_access=False)
    await db.commit()
    return {"Authorization": f"Bearer {raw}"}


async def test_batches_per_account_are_limited(client: AsyncClient, db_session: AsyncSession) -> None:
    first = await _key(db_session, "ingrl-one")
    second = await _key(db_session, "ingrl-two")
    # Not even a valid batch: the limit counts requests before the body is read.
    statuses = [(await client.post(URL, content=b"{}", headers=first)).status_code for _ in range(3)]
    assert statuses == [400, 400, 429]

    resp = await client.post(URL, content=b"{}", headers=first)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1
    assert resp.json()["errors"][0]["code"] == "rate_limit_exceeded"

    # Another account has its own budget, and reading the limits is not metered.
    assert (await client.post(URL, content=b"{}", headers=second)).status_code == 400
    assert (await client.get(URL, headers=first)).status_code == 200
    info = (await client.get(URL, headers=first)).json()
    assert info["rate_limit"] == {"requests": 2, "window_seconds": 60}
