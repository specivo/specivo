"""Redis-backed metadata store for MCP Streamable HTTP sessions.

Background
----------
The Python ``mcp`` SDK's ``StreamableHTTPSessionManager`` keeps live
``StreamableHTTPServerTransport`` instances in a plain in-memory
``dict[str, StreamableHTTPServerTransport]`` (see
``_server_instances``). Each transport wraps live anyio streams and a
running task in the manager's task group; these objects are
**fundamentally not serializable** and cannot be reconstructed across
an api process restart. That is an SDK constraint, not something we
can work around by swapping in a Redis-backed dict.

What this module does
---------------------
This module provides a **parallel metadata store** for sessions,
keyed by ``Mcp-Session-Id``. It stores the durable bits we actually
care about for operations and telemetry:

* ``api_key_id`` / ``user_id`` — for attribution
* ``created_at`` / ``last_seen_at`` — for idle expiration
* caller-supplied extras (user_agent, transport type)

The Redis entries have a sliding TTL (default 7 days, configurable
via ``mcp_session_ttl_seconds``): every ``touch()`` resets the TTL to
the configured value. If a session has not been touched within that
window, Redis expires it automatically.

What this module does **not** do
--------------------------------
It does **not** rehydrate live MCP transports after an api restart.
On restart, any open client connections are forcibly reset: the
anyio streams are closed, the task group is cancelled, and the
in-memory ``_server_instances`` dict is cleared. The Redis entries
for those sessions will still exist until TTL expiry, which means a
client that re-issues its old ``Mcp-Session-Id`` after restart will
find the SDK's in-memory dict empty and still receive a 404 from the
SDK's stateful path. That is the SDK's behaviour and this layer
does not change it.

The value of the Redis layer is:

1. **Attribution** — after a restart we still know which api key / user
   each recorded session belonged to, for audit trails and telemetry.
2. **Foundation** — the upcoming MCP telemetry pipeline (wiki page
   ``mcp-tool-call-telemetry``) can read and extend these entries
   without needing a second storage layer.
3. **Operational visibility** — ``exists()`` / ``get()`` let ops tools
   introspect session state without touching the SDK internals.

Interaction with ``agent_sessions``
-----------------------------------
The Postgres ``agent_sessions`` table tracks **long-term agent
identity** (one row per ``(api_key_id, user_agent)``). That is a
different concept from the ephemeral MCP transport sessions keyed
here. This store does not replace ``agent_sessions``; it only stores
``api_key_id`` / ``user_id`` in each Redis hash so that later
consumers can join back to the durable identity record.
"""

from __future__ import annotations

import time
from typing import Any

from redis.asyncio import Redis

_KEY_PREFIX = "mcp:session:"


def _key(session_id: str) -> str:
    return f"{_KEY_PREFIX}{session_id}"


class RedisSessionStore:
    """Metadata store for MCP Streamable HTTP sessions, backed by Redis.

    All methods are async; the store holds a reference to an existing
    ``redis.asyncio.Redis`` client (typically the shared app client from
    ``specivo.core.redis.get_redis``). The store does not own the client
    and does not close it.

    Storage layout::

        mcp:session:<id>   HASH   {
            api_key_id, user_id, created_at, last_seen_at,
            user_agent, transport, ...
        }
                           TTL    sliding; reset to ``ttl_seconds`` on
                                  every ``touch()`` / ``create()`` /
                                  ``get()`` call.

    Sliding TTL note: ``get()`` refreshes the TTL as a side effect,
    because any successful session lookup is evidence that the session
    is still in use. Callers that want a pure read without extending
    the TTL should use ``peek()`` instead.
    """

    def __init__(self, redis: Redis, *, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._redis = redis
        self._ttl = ttl_seconds

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    async def create(self, session_id: str, metadata: dict[str, Any]) -> None:
        """Record a new session with ``metadata`` and start the sliding TTL.

        Overwrites any existing entry for ``session_id`` without warning —
        collisions are expected to be astronomically rare with UUID4 ids,
        and the SDK itself treats session ids as unique.
        """
        now = int(time.time())
        payload: dict[str, str] = {
            "created_at": str(now),
            "last_seen_at": str(now),
        }
        for k, v in metadata.items():
            if v is None:
                continue
            payload[str(k)] = str(v)
        key = _key(session_id)
        # Pipeline HSET + EXPIRE so both land atomically.
        pipe = self._redis.pipeline()
        pipe.delete(key)
        pipe.hset(key, mapping=payload)
        pipe.expire(key, self._ttl)
        await pipe.execute()

    async def peek(self, session_id: str) -> dict[str, str] | None:
        """Return the stored metadata without refreshing the TTL.

        Returns ``None`` if the session does not exist or has expired.
        """
        raw = await self._redis.hgetall(_key(session_id))
        if not raw:
            return None
        return dict(raw)

    async def get(self, session_id: str) -> dict[str, str] | None:
        """Return the stored metadata and refresh the sliding TTL.

        Returns ``None`` if the session does not exist or has expired.
        Updates ``last_seen_at`` to the current wall-clock time on every
        successful read.
        """
        key = _key(session_id)
        raw = await self._redis.hgetall(key)
        if not raw:
            return None
        now = int(time.time())
        pipe = self._redis.pipeline()
        pipe.hset(key, "last_seen_at", str(now))
        pipe.expire(key, self._ttl)
        await pipe.execute()
        raw["last_seen_at"] = str(now)
        return dict(raw)

    async def touch(self, session_id: str) -> bool:
        """Refresh the sliding TTL without reading the payload.

        Returns ``True`` if the session exists, ``False`` otherwise.
        """
        key = _key(session_id)
        exists = await self._redis.exists(key)
        if not exists:
            return False
        now = int(time.time())
        pipe = self._redis.pipeline()
        pipe.hset(key, "last_seen_at", str(now))
        pipe.expire(key, self._ttl)
        await pipe.execute()
        return True

    async def delete(self, session_id: str) -> None:
        """Remove the session entry. No-op if the session does not exist."""
        await self._redis.delete(_key(session_id))

    async def exists(self, session_id: str) -> bool:
        """Return ``True`` if the session entry is still present in Redis."""
        return bool(await self._redis.exists(_key(session_id)))


_store: RedisSessionStore | None = None


async def get_mcp_session_store() -> RedisSessionStore:
    """Return the application-wide :class:`RedisSessionStore` singleton.

    Lazily constructs the store on first call using the shared Redis
    client from :func:`specivo.core.redis.get_redis` and the
    ``mcp_session_ttl_seconds`` setting.
    """
    global _store  # noqa: PLW0603
    if _store is None:
        # Local imports to avoid a circular dependency at module import
        # time: ``specivo.core.redis`` does not depend on anything MCP.
        from specivo.core.config import get_settings
        from specivo.core.redis import get_redis

        settings = get_settings()
        redis = await get_redis()
        _store = RedisSessionStore(
            redis, ttl_seconds=settings.mcp_session_ttl_seconds
        )
    return _store


def reset_mcp_session_store() -> None:
    """Clear the cached singleton. Test-only helper."""
    global _store  # noqa: PLW0603
    _store = None


async def check_redis_persistence(redis: Redis) -> bool:
    """Return ``True`` if Redis has AOF persistence enabled.

    Used by the startup hook to emit a warning when the bundled /
    external Redis instance is running without AOF. When persistence
    is off, every Redis restart drops MCP session metadata, which
    means the attribution benefit of this store is lost across
    restarts (though the live transports were going to die anyway, so
    the user-visible effect is the same 404 path).

    Gracefully returns ``True`` if the call fails for any reason —
    the goal is to warn the operator when we are **certain**
    persistence is off, not to cry wolf on transient Redis errors.
    """
    try:
        cfg = await redis.config_get("appendonly")
    except Exception:
        return True
    if not cfg:
        return True
    value = cfg.get("appendonly", "").lower() if isinstance(cfg, dict) else ""
    return value == "yes"
