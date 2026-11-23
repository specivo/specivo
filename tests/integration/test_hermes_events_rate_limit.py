"""The Hermes event receiver's per-address rate limit (Redis-backed, so serial).

Known and unknown receivers share one bucket per client address, checked
before the token is looked up or the body is verified, so someone who learned
a receiver URL cannot spend more than the address budget on signature checks.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.services.hermes_receiver_service import HermesReceiverService
from tests.factories.user import ServiceAccountFactory
from tests.fixtures.hermes_events import payload, post_event

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6380/0")


@pytest_asyncio.fixture(autouse=True)
async def clean_buckets(monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    r = aioredis.from_url(_REDIS_URL, decode_responses=True)

    async def _clear() -> None:
        keys = await r.keys("rl:hermes_events*")
        if keys:
            await r.delete(*keys)

    await _clear()
    yield
    await _clear()
    await r.aclose()


async def test_a_receiver_is_limited_per_address(client: AsyncClient, db_session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "hermes_event_rate_limit_per_minute", 2)
    owner = ServiceAccountFactory.build(login="hrate_owner", status="active")
    db_session.add(owner)
    await db_session.flush()
    minted = await HermesReceiverService().create(db_session, owner=owner, name="rate", actor=owner)
    await db_session.commit()

    statuses = [
        (await post_event(client, minted.token, payload("on_session_start", f"rate-{i}"), minted.secret)).status_code
        for i in range(3)
    ]
    assert statuses == [200, 200, 429]


async def test_unknown_tokens_and_bad_signatures_spend_the_same_bucket(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "hermes_event_rate_limit_per_minute", 2)
    owner = ServiceAccountFactory.build(login="hrate_owner2", status="active")
    db_session.add(owner)
    await db_session.flush()
    minted = await HermesReceiverService().create(db_session, owner=owner, name="rate", actor=owner)
    await db_session.commit()

    unknown = await post_event(client, "u" * 43, payload("on_session_start", "r"), "x")
    forged = await post_event(client, minted.token, payload("on_session_start", "r"), "wrong")
    real = await post_event(client, minted.token, payload("on_session_start", "r"), minted.secret)
    assert [unknown.status_code, forged.status_code, real.status_code] == [404, 401, 429]
    assert "Retry-After" in real.headers
