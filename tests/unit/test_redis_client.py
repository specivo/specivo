"""The shared Redis client follows the event loop it is used from.

A Celery worker runs every task in a new event loop, and pytest gives each test
its own. Connections opened on a finished loop fail with "Event loop is closed",
which the JWT blocklist check turns into a refusal, so a client left over from
an earlier loop must never be handed out.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

import specivo.core.redis as redis_module
from specivo.core.redis import get_redis

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_client() -> Iterator[None]:
    """Start and end without a cached client, so no other test inherits one."""
    redis_module._redis = None
    redis_module._redis_loop = None
    yield
    redis_module._redis = None
    redis_module._redis_loop = None


def test_one_loop_reuses_one_client() -> None:
    async def twice() -> tuple[object, object]:
        return await get_redis(), await get_redis()

    first, second = asyncio.run(twice())

    assert first is second


def test_a_new_loop_gets_a_new_client() -> None:
    first = asyncio.run(get_redis())
    second = asyncio.run(get_redis())

    assert first is not second
