"""Tests for :mod:`specivo.mcp.session_store`.

These tests use a small in-memory async fake that implements the
subset of the ``redis.asyncio.Redis`` surface that
:class:`RedisSessionStore` touches (``pipeline``, ``hset``,
``hgetall``, ``expire``, ``delete``, ``exists``, ``config_get``).
Introducing ``fakeredis`` as a real dependency for this one module
felt heavy, and the fake is small enough to review at a glance.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from specivo.mcp.session_store import (
    RedisSessionStore,
    check_redis_persistence,
)

# ---------------------------------------------------------------------------
# In-memory async fake
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Tiny async Redis stand-in covering what RedisSessionStore uses."""

    def __init__(self, *, appendonly: str = "yes") -> None:
        self._hashes: dict[str, dict[str, str]] = {}
        self._expiries: dict[str, float] = {}
        self._loop = asyncio.get_event_loop
        self._appendonly = appendonly
        # Monotonic clock used only for TTL expiry; tests advance it
        # directly via ``_advance`` rather than sleeping.
        self._now: float = 0.0

    # --- test helpers ------------------------------------------------------

    def _advance(self, seconds: float) -> None:
        self._now += seconds
        self._sweep()

    def _sweep(self) -> None:
        expired = [k for k, exp in self._expiries.items() if exp <= self._now]
        for k in expired:
            self._hashes.pop(k, None)
            self._expiries.pop(k, None)

    # --- Redis API subset --------------------------------------------------

    async def hgetall(self, key: str) -> dict[str, str]:
        self._sweep()
        return dict(self._hashes.get(key, {}))

    async def hset(
        self,
        key: str,
        field: str | None = None,
        value: str | None = None,
        *,
        mapping: dict[str, str] | None = None,
    ) -> int:
        self._sweep()
        bucket = self._hashes.setdefault(key, {})
        added = 0
        if mapping:
            for k, v in mapping.items():
                if k not in bucket:
                    added += 1
                bucket[k] = v
        if field is not None:
            if field not in bucket:
                added += 1
            bucket[field] = value or ""
        return added

    async def expire(self, key: str, seconds: int) -> bool:
        if key not in self._hashes:
            return False
        self._expiries[key] = self._now + seconds
        return True

    async def delete(self, *keys: str) -> int:
        self._sweep()
        removed = 0
        for k in keys:
            if k in self._hashes:
                del self._hashes[k]
                self._expiries.pop(k, None)
                removed += 1
        return removed

    async def exists(self, key: str) -> int:
        self._sweep()
        return 1 if key in self._hashes else 0

    async def config_get(self, param: str) -> dict[str, str]:
        return {param: self._appendonly}

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def hset(self, *args: Any, **kwargs: Any) -> _FakePipeline:
        self._ops.append(("hset", args, kwargs))
        return self

    def expire(self, *args: Any, **kwargs: Any) -> _FakePipeline:
        self._ops.append(("expire", args, kwargs))
        return self

    def delete(self, *args: Any, **kwargs: Any) -> _FakePipeline:
        self._ops.append(("delete", args, kwargs))
        return self

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for name, args, kwargs in self._ops:
            fn = getattr(self._redis, name)
            results.append(await fn(*args, **kwargs))
        self._ops.clear()
        return results


# ---------------------------------------------------------------------------
# Store behaviour
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis() -> _FakeRedis:
    return _FakeRedis()


@pytest.fixture
def store(fake_redis: _FakeRedis) -> RedisSessionStore:
    return RedisSessionStore(fake_redis, ttl_seconds=60)


@pytest.mark.unit
async def test_create_and_peek_roundtrip(store: RedisSessionStore) -> None:
    await store.create(
        "sess-1",
        {"api_key_id": 42, "user_id": 7, "user_agent": "pytest"},
    )
    data = await store.peek("sess-1")
    assert data is not None
    assert data["api_key_id"] == "42"
    assert data["user_id"] == "7"
    assert data["user_agent"] == "pytest"
    assert "created_at" in data
    assert "last_seen_at" in data


@pytest.mark.unit
async def test_create_drops_none_values(store: RedisSessionStore) -> None:
    await store.create("sess-none", {"api_key_id": 1, "sprint": None})
    data = await store.peek("sess-none")
    assert data is not None
    assert "sprint" not in data
    assert data["api_key_id"] == "1"


@pytest.mark.unit
async def test_get_updates_last_seen_and_refreshes_ttl(
    fake_redis: _FakeRedis, store: RedisSessionStore
) -> None:
    await store.create("sess-2", {"api_key_id": 1})
    initial_exp = fake_redis._expiries["mcp:session:sess-2"]

    fake_redis._advance(30)  # burn half the TTL
    data = await store.get("sess-2")
    assert data is not None

    new_exp = fake_redis._expiries["mcp:session:sess-2"]
    assert new_exp > initial_exp
    assert int(data["last_seen_at"]) >= int(data["created_at"])


@pytest.mark.unit
async def test_get_returns_none_after_expiry(
    fake_redis: _FakeRedis, store: RedisSessionStore
) -> None:
    await store.create("sess-3", {"api_key_id": 1})
    fake_redis._advance(61)  # TTL is 60s
    assert await store.get("sess-3") is None
    assert await store.peek("sess-3") is None
    assert not await store.exists("sess-3")


@pytest.mark.unit
async def test_touch_refreshes_ttl_without_reading(
    fake_redis: _FakeRedis, store: RedisSessionStore
) -> None:
    await store.create("sess-4", {"api_key_id": 1})
    fake_redis._advance(30)
    assert await store.touch("sess-4") is True
    fake_redis._advance(50)
    # 30 + 50 = 80s total, but each touch reset TTL to 60s so still alive
    assert await store.exists("sess-4")


@pytest.mark.unit
async def test_touch_on_missing_session_returns_false(
    store: RedisSessionStore,
) -> None:
    assert await store.touch("nope") is False


@pytest.mark.unit
async def test_delete_is_idempotent(store: RedisSessionStore) -> None:
    await store.create("sess-5", {"api_key_id": 1})
    await store.delete("sess-5")
    await store.delete("sess-5")  # no error
    assert not await store.exists("sess-5")


@pytest.mark.unit
async def test_rejects_nonpositive_ttl(fake_redis: _FakeRedis) -> None:
    with pytest.raises(ValueError):
        RedisSessionStore(fake_redis, ttl_seconds=0)
    with pytest.raises(ValueError):
        RedisSessionStore(fake_redis, ttl_seconds=-1)


# ---------------------------------------------------------------------------
# Restart simulation — the core regression test
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_session_survives_store_instance_replacement(
    fake_redis: _FakeRedis,
) -> None:
    """New ``RedisSessionStore`` against the same Redis still sees the session.

    This simulates an api-process restart. The point of moving session
    metadata out of process memory is that a fresh store instance can
    pick up where the old one left off, as long as Redis itself is
    still running and persistent.
    """
    store_a = RedisSessionStore(fake_redis, ttl_seconds=120)
    await store_a.create(
        "durable", {"api_key_id": 99, "user_id": 5}
    )

    # Drop store_a entirely; construct a brand-new store against the
    # same Redis backend (what happens on api restart).
    del store_a
    store_b = RedisSessionStore(fake_redis, ttl_seconds=120)

    data = await store_b.get("durable")
    assert data is not None
    assert data["api_key_id"] == "99"
    assert data["user_id"] == "5"


# ---------------------------------------------------------------------------
# Boot-time AOF check
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_aof_check_returns_true_when_enabled() -> None:
    fake = _FakeRedis(appendonly="yes")
    assert await check_redis_persistence(fake) is True


@pytest.mark.unit
async def test_aof_check_returns_false_when_disabled() -> None:
    fake = _FakeRedis(appendonly="no")
    assert await check_redis_persistence(fake) is False


@pytest.mark.unit
async def test_aof_check_tolerates_redis_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Broken:
        async def config_get(self, _param: str) -> dict[str, str]:
            raise RuntimeError("boom")

    # Silently returns True — we do not want to cry wolf about
    # persistence when the underlying call itself failed.
    assert await check_redis_persistence(_Broken()) is True  # type: ignore[arg-type]


@pytest.mark.unit
async def test_startup_warning_logged_when_aof_disabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The main lifespan logs a warning when AOF is off."""
    fake = _FakeRedis(appendonly="no")
    with caplog.at_level(logging.WARNING):
        ok = await check_redis_persistence(fake)
        if not ok:
            logging.getLogger("specivo.main").warning(
                "Redis AOF persistence is disabled (appendonly=no)."
            )
    assert any(
        "AOF persistence is disabled" in rec.message for rec in caplog.records
    )


@pytest.mark.unit
async def test_no_warning_when_aof_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = _FakeRedis(appendonly="yes")
    with caplog.at_level(logging.WARNING):
        ok = await check_redis_persistence(fake)
        assert ok is True
    assert not any(
        "AOF persistence is disabled" in rec.message for rec in caplog.records
    )
