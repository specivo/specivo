"""Rate limits on the repository webhook endpoint (Redis-backed, so serial)."""

from __future__ import annotations

import json
import os
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from tests.factories.project import ProjectFactory
from tests.fixtures.forge_hooks import commit, hook_path, make_integration, push_payload, signed_headers

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6380/0")


@pytest_asyncio.fixture(autouse=True)
async def clean_buckets(monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    monkeypatch.setattr("specivo.hooks.router.queue_processing", lambda ids: 0)
    r = aioredis.from_url(_REDIS_URL, decode_responses=True)

    async def _clear() -> None:
        keys = await r.keys("rl:git_hook*")
        if keys:
            await r.delete(*keys)

    await _clear()
    yield
    await _clear()
    await r.aclose()


async def test_per_integration_limit(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(get_settings(), "git_hook_rate_limit_per_minute", 3)
    project = ProjectFactory.build(key="RRATE", identifier="rrate", name="Rate")
    db_session.add(project)
    await db_session.commit()
    integration = await make_integration(db_session, project)
    statuses = []
    for i in range(4):
        body = json.dumps(push_payload("github", [commit(f"r{i}", "no refs")])).encode()
        resp = await client.post(hook_path(integration), content=body, headers=signed_headers("github", body))
        statuses.append(resp.status_code)
    assert statuses == [202, 202, 202, 429]
    assert "Retry-After" in resp.headers


async def test_unknown_ids_are_limited_per_client(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(get_settings(), "git_hook_unknown_rate_limit_per_minute", 2)
    statuses = [(await client.post(f"/hooks/github/{uuid.uuid4()}/", content=b"{}")).status_code for _ in range(3)]
    assert statuses == [404, 404, 429]
