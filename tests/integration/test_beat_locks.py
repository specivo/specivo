"""The locks that keep periodic (beat) tasks to one run at a time.

They must live in the Redis the settings name (``settings.redis_url``, as
every other Redis user does), not in one built from the ``REDIS_URL``
environment variable, which is unset when the URL comes from ``.env``.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from specivo.core.config import get_settings
from specivo.core.locks import beat_lock
from specivo.core.redis import get_redis
from specivo.tasks import cleanup, recurring

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

# Nothing listens on the discard port: a client built from this URL cannot connect.
_UNREACHABLE = "redis://127.0.0.1:9/0"


@pytest.fixture
def env_points_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """The settings keep the test Redis while ``REDIS_URL`` in the environment points nowhere."""
    get_settings()  # cached with the real URL before the environment changes
    monkeypatch.setenv("REDIS_URL", _UNREACHABLE)


def _unique_key() -> str:
    return f"specivo:test_beat_lock:{uuid.uuid4().hex}"


class TestBeatLock:
    async def test_takes_and_releases_the_key(self) -> None:
        key = _unique_key()
        redis = await get_redis()
        async with beat_lock(key, 60) as acquired:
            assert acquired
            assert await redis.get(key) is not None
            assert 0 < await redis.ttl(key) <= 60
        assert await redis.get(key) is None

    async def test_not_acquired_while_another_run_holds_it(self) -> None:
        key = _unique_key()
        redis = await get_redis()
        await redis.set(key, "other-run", ex=60)
        async with beat_lock(key, 60) as acquired:
            assert not acquired
        # Someone else's lock is left alone.
        assert await redis.get(key) == "other-run"

    async def test_does_not_release_a_lock_taken_over_after_expiry(self) -> None:
        key = _unique_key()
        redis = await get_redis()
        async with beat_lock(key, 60) as acquired:
            assert acquired
            await redis.set(key, "next-run", ex=60)  # ours expired and another run took it
        assert await redis.get(key) == "next-run"


class TestRecurringLock:
    async def test_uses_the_configured_redis(self, env_points_elsewhere: None, monkeypatch: pytest.MonkeyPatch) -> None:
        key = _unique_key()
        monkeypatch.setattr(recurring, "_LOCK_KEY", key)
        held_during_run: list[bool] = []

        async def fake_generate(_session, _now):  # type: ignore[no-untyped-def]
            held_during_run.append(await (await get_redis()).get(key) is not None)
            return 0, 0, 0

        @asynccontextmanager
        async def fake_session():  # type: ignore[no-untyped-def]
            yield MagicMock()

        monkeypatch.setattr(recurring, "_generate_for_session", fake_generate)
        monkeypatch.setattr("specivo.tasks._async.task_session", fake_session)

        await recurring._generate_recurring_async()

        assert held_during_run == [True]
        assert await (await get_redis()).get(key) is None

    async def test_skips_while_another_run_holds_the_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = _unique_key()
        monkeypatch.setattr(recurring, "_LOCK_KEY", key)
        await (await get_redis()).set(key, "other-run", ex=60)
        generate = AsyncMock()
        monkeypatch.setattr(recurring, "_generate_for_session", generate)

        await recurring._generate_recurring_async()

        generate.assert_not_awaited()


class TestCleanupLock:
    async def test_uses_the_configured_redis(self, env_points_elsewhere: None, monkeypatch: pytest.MonkeyPatch) -> None:
        key = _unique_key()
        monkeypatch.setattr(cleanup, "_LOCK_KEY", key)
        held_during_run: list[bool] = []

        session = MagicMock()

        async def execute(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            held_during_run.append(await (await get_redis()).get(key) is not None)
            return MagicMock(rowcount=0)

        session.execute = execute
        session.commit = AsyncMock()

        @asynccontextmanager
        async def fake_session():  # type: ignore[no-untyped-def]
            yield session

        with (
            patch("specivo.tasks._async.task_session", fake_session),
            patch("specivo.services.repository_hooks.purge_old_deliveries", AsyncMock(return_value=0)),
            patch("specivo.services.webhook_delivery.purge_old_deliveries", AsyncMock(return_value=0)),
        ):
            await cleanup._cleanup_async()

        assert held_during_run and all(held_during_run)
        assert await (await get_redis()).get(key) is None

    async def test_skips_while_another_run_holds_the_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = _unique_key()
        monkeypatch.setattr(cleanup, "_LOCK_KEY", key)
        await (await get_redis()).set(key, "other-run", ex=60)
        session_factory = MagicMock()
        monkeypatch.setattr("specivo.tasks._async.task_session", session_factory)

        await cleanup._cleanup_async()

        session_factory.assert_not_called()
