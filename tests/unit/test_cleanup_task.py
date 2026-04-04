"""Unit tests for the token cleanup periodic task.

Covers:
- Beat schedule is configured with correct task and interval
- cleanup_expired_tokens task exists and is registered
"""

from __future__ import annotations

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
