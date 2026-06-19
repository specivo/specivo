"""Regression: the wiki-link rebuild task tolerates a Redis lock lost mid-run.

Under heavy worker contention the per-wiki Redis lock can expire (or be
re-acquired by another worker) before the rebuild finishes. The rebuild body
has already committed by the time the ``async with`` block exits, but releasing
the now-expired lock raises ``LockNotOwnedError``. The task must swallow that
error rather than let it propagate into a Celery retry (which would re-run work
that already succeeded).

A genuine *acquire* failure (a plain ``LockError`` from ``__aenter__``) must
still propagate so the on-demand task is retried — unlike the periodic beat
tasks, skipping an on-demand rebuild would leave a page's links stale.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import LockError, LockNotOwnedError

from specivo.tasks.wiki_links import _rebuild_links_async

pytestmark = pytest.mark.unit


class _Lock:
    """Async-context-manager lock whose release raises ``release_exc`` (or not)."""

    def __init__(self, release_exc: Exception | None = None) -> None:
        self._release_exc = release_exc

    async def __aenter__(self) -> _Lock:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        if self._release_exc is not None:
            raise self._release_exc
        return False


class _AcquireFailLock:
    """Lock that fails to acquire, like redis-py when ``blocking_timeout`` elapses."""

    async def __aenter__(self) -> _AcquireFailLock:
        raise LockError("Unable to acquire lock within the time specified")

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


@pytest.fixture
def mock_rebuild_body(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stub out the DB-touching body so only lock handling is exercised."""
    service = MagicMock()
    service.rebuild_page_links = AsyncMock(return_value=0)
    service.resolve_incoming_links = AsyncMock()
    monkeypatch.setattr("specivo.services.wiki_link_service.WikiLinkService", lambda: service)

    session = MagicMock()
    session.get = AsyncMock(return_value=None)  # page lookup -> skip incoming-link resolution
    session.commit = AsyncMock()

    @asynccontextmanager
    async def _fake_session():  # type: ignore[no-untyped-def]
        yield session

    monkeypatch.setattr("specivo.tasks._async.task_session", _fake_session)
    return service


def _patch_redis(monkeypatch: pytest.MonkeyPatch, lock: object) -> MagicMock:
    client = MagicMock()
    client.lock = MagicMock(return_value=lock)
    client.aclose = AsyncMock()
    monkeypatch.setattr("redis.asyncio.from_url", lambda *a, **k: client)
    return client


async def test_lock_lost_on_release_is_swallowed(
    monkeypatch: pytest.MonkeyPatch, mock_rebuild_body: MagicMock
) -> None:
    """A lock that expired mid-run (LockNotOwnedError on release) must not propagate."""
    client = _patch_redis(monkeypatch, _Lock(LockNotOwnedError("Cannot release a lock that's no longer owned")))

    # Must not raise: the rebuild already committed inside the lock.
    await _rebuild_links_async(1, 1)

    mock_rebuild_body.rebuild_page_links.assert_awaited_once()
    client.aclose.assert_awaited_once()  # redis client still cleaned up


async def test_acquire_failure_still_propagates(
    monkeypatch: pytest.MonkeyPatch, mock_rebuild_body: MagicMock
) -> None:
    """Failing to acquire the lock must propagate so the task is retried."""
    _patch_redis(monkeypatch, _AcquireFailLock())

    with pytest.raises(LockError) as exc_info:
        await _rebuild_links_async(1, 1)

    # A plain acquire failure, not a lost-lock release error.
    assert not isinstance(exc_info.value, LockNotOwnedError)
    mock_rebuild_body.rebuild_page_links.assert_not_awaited()
