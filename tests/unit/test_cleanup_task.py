"""Unit tests for the token cleanup periodic task.

Covers:
- Beat schedule is configured with correct task and interval
- cleanup_expired_tokens task exists and is registered
- The cleanup run purges both delivery logs and logs the counts
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


class TestCleanupBeatSchedule:
    def test_beat_schedule_has_cleanup_task(self):
        """The Celery beat schedule must include the cleanup task."""
        from specivo.tasks import celery_app

        schedule = celery_app.conf.beat_schedule
        assert "cleanup-expired-tokens" in schedule

    def test_cleanup_task_runs_every_hour(self):
        """The cleanup task must be scheduled to run every 3600 seconds."""
        from specivo.tasks import celery_app

        entry = celery_app.conf.beat_schedule["cleanup-expired-tokens"]
        assert entry["schedule"] == 3600

    def test_cleanup_task_references_correct_task(self):
        """The schedule entry must reference the correct task path."""
        from specivo.tasks import celery_app

        entry = celery_app.conf.beat_schedule["cleanup-expired-tokens"]
        assert entry["task"] == "specivo.tasks.cleanup.cleanup_expired_tokens"

    def test_cleanup_task_is_importable(self):
        """The cleanup task function must be importable."""
        from specivo.tasks.cleanup import cleanup_expired_tokens

        assert callable(cleanup_expired_tokens)


class _FakeRedis:
    def lock(self, *_args, **_kwargs):
        @asynccontextmanager
        async def held():
            yield

        return held()

    async def aclose(self) -> None:
        pass


class TestCleanupRun:
    async def test_purges_both_delivery_logs_and_logs_the_counts(self, caplog):
        from specivo.tasks.cleanup import _cleanup_async

        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(rowcount=0))
        session.commit = AsyncMock()

        @asynccontextmanager
        async def fake_session():
            yield session

        repository_purge = AsyncMock(return_value=2)
        webhook_purge = AsyncMock(return_value=5)
        with (
            patch("specivo.tasks.cleanup.aioredis.from_url", return_value=_FakeRedis()),
            patch("specivo.tasks._async.task_session", fake_session),
            patch("specivo.services.repository_hooks.purge_old_deliveries", repository_purge),
            patch("specivo.services.webhook_delivery.purge_old_deliveries", webhook_purge),
            caplog.at_level(logging.INFO, logger="specivo.tasks.cleanup"),
        ):
            await _cleanup_async()

        repository_purge.assert_awaited_once_with(session)
        webhook_purge.assert_awaited_once_with(session)
        assert "Repository delivery cleanup: deleted 2 old deliveries" in caplog.text
        assert "Webhook delivery cleanup: deleted 5 old deliveries" in caplog.text
