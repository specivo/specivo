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


@asynccontextmanager
async def _held_lock(*_args, **_kwargs):
    yield True


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
            patch("specivo.tasks.cleanup.beat_lock", _held_lock),
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


class TestWikiTrashPurgeBeatSchedule:
    def test_beat_schedule_purges_the_wiki_trash_daily(self):
        from specivo.tasks import celery_app

        entry = celery_app.conf.beat_schedule["purge-wiki-trash"]
        assert entry["task"] == "specivo.tasks.cleanup.purge_wiki_trash"
        assert entry["schedule"] == 86400

    def test_task_is_registered(self):
        from specivo.tasks import celery_app

        assert "specivo.tasks.cleanup.purge_wiki_trash" in celery_app.tasks


@asynccontextmanager
async def _lock_held_elsewhere(*_args, **_kwargs):
    yield False


def _purge_session():
    session = MagicMock()
    session.commit = AsyncMock()

    @asynccontextmanager
    async def fake_session():
        yield session

    return session, fake_session


def _settings(retention_days: int) -> MagicMock:
    return MagicMock(wiki_trash_retention_days=retention_days)


class TestWikiTrashPurgeRun:
    async def test_purges_with_the_configured_retention_and_removes_files_after_commit(self, tmp_path, caplog):
        from specivo.services.wiki_service import WikiTrashPurge
        from specivo.tasks.cleanup import _purge_wiki_trash_async

        stored = tmp_path / "stored.bin"
        stored.write_bytes(b"x")
        session, fake_session = _purge_session()
        seen_at_commit: list[bool] = []
        session.commit.side_effect = lambda: seen_at_commit.append(stored.exists())
        purge = AsyncMock(return_value=WikiTrashPurge(page_ids=[4, 7], files=[stored, tmp_path / "already-gone"]))

        with (
            patch("specivo.tasks.cleanup.beat_lock", _held_lock),
            patch("specivo.tasks._async.task_session", fake_session),
            patch("specivo.core.config.get_settings", return_value=_settings(30)),
            patch("specivo.services.wiki_service.WikiService.hard_delete_expired_pages", purge),
            caplog.at_level(logging.INFO, logger="specivo.tasks.cleanup"),
        ):
            await _purge_wiki_trash_async()

        purge.assert_awaited_once_with(session, retention_days=30)
        assert seen_at_commit == [True]  # the file was still there when the rows were committed
        assert not stored.exists()
        assert "Wiki trash purge: deleted 2 pages" in caplog.text

    async def test_zero_retention_keeps_the_trash(self):
        from specivo.tasks.cleanup import _purge_wiki_trash_async

        purge = AsyncMock()
        with (
            patch("specivo.tasks.cleanup.beat_lock", _held_lock),
            patch("specivo.core.config.get_settings", return_value=_settings(0)),
            patch("specivo.services.wiki_service.WikiService.hard_delete_expired_pages", purge),
        ):
            await _purge_wiki_trash_async()

        purge.assert_not_awaited()

    async def test_skips_while_another_run_holds_the_lock(self):
        from specivo.tasks.cleanup import _purge_wiki_trash_async

        purge = AsyncMock()
        with (
            patch("specivo.tasks.cleanup.beat_lock", _lock_held_elsewhere),
            patch("specivo.core.config.get_settings", return_value=_settings(90)),
            patch("specivo.services.wiki_service.WikiService.hard_delete_expired_pages", purge),
        ):
            await _purge_wiki_trash_async()

        purge.assert_not_awaited()


class TestWikiTrashRetentionSetting:
    def test_defaults_to_ninety_days(self):
        from specivo.core.config import Settings

        assert Settings.model_fields["wiki_trash_retention_days"].default == 90
