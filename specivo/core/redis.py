"""Redis connection for caching, rate limiting, and pub/sub."""

import asyncio

import redis.asyncio as redis
from redis.asyncio import Redis

from specivo.core.config import get_settings

_redis: Redis | None = None
_redis_loop: asyncio.AbstractEventLoop | None = None


async def get_redis() -> Redis:
    """Return the shared Redis client, creating it on first call.

    The client is backed by a connection pool (max_connections=20).
    Connections are reused across requests — do NOT call ``close()`` per
    request; use ``close_redis()`` only at application shutdown.

    Pooled connections belong to the event loop they were opened on. The API
    server runs one loop, but a Celery worker runs each task in a new loop
    (see ``specivo.tasks._async``), and a connection from a finished loop fails
    with "Event loop is closed". The client is therefore replaced when it is
    asked for from a different loop than the one it was created on. The old
    client is dropped rather than closed, since its loop can no longer run.
    """
    global _redis, _redis_loop
    loop = asyncio.get_running_loop()
    if _redis is None or _redis_loop is not loop:
        settings = get_settings()
        _redis = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=20,
        )
        _redis_loop = loop
    return _redis


async def close_redis() -> None:
    """Close the Redis connection pool. Called from the lifespan shutdown hook."""
    global _redis, _redis_loop
    if _redis is not None:
        await _redis.aclose()
        _redis = None
        _redis_loop = None
