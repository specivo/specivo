"""Redis connection for caching, rate limiting, and pub/sub."""

import redis.asyncio as redis
from redis.asyncio import Redis

from specivo.core.config import get_settings

_redis: Redis | None = None


async def get_redis() -> Redis:
    """Return the shared Redis client, creating it on first call.

    The client is backed by a connection pool (max_connections=20).
    Connections are reused across requests — do NOT call ``close()`` per
    request; use ``close_redis()`` only at application shutdown.
    """
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=20,
        )
    return _redis


async def close_redis() -> None:
    """Close the Redis connection pool. Called from the lifespan shutdown hook."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
