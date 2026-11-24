"""The hourly AI spend budget check: its beat entry, its lock, and that mails go out only after the commit.

The check itself (``AiSpendBudgetService.check``) is tested against the
database in ``tests/services/test_ai_spend_budgets``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from specivo.services.ai_spend_budget_service import BudgetCheck, PendingAlert

pytestmark = pytest.mark.unit


@asynccontextmanager
async def _held_lock(*_args, **_kwargs):
    yield True


@asynccontextmanager
async def _lock_held_elsewhere(*_args, **_kwargs):
    yield False


def _session():
    session = MagicMock()
    session.commit = AsyncMock()

    @asynccontextmanager
    async def fake_session():
        yield session

    return session, fake_session


class TestBeatSchedule:
    def test_the_check_runs_hourly(self):
        from specivo.tasks import celery_app

        entry = celery_app.conf.beat_schedule["check-ai-spend-budgets"]
        assert entry["task"] == "specivo.tasks.ai_usage.check_ai_spend_budgets"
        assert entry["schedule"] == 3600

    def test_task_is_registered(self):
        from specivo.tasks import celery_app

        assert "specivo.tasks.ai_usage.check_ai_spend_budgets" in celery_app.tasks


class TestCheckRun:
    async def test_mails_are_queued_after_the_alerts_are_committed(self, caplog):
        from specivo.tasks.ai_usage import check_ai_spend_budgets_async

        session, fake_session = _session()
        order: list[str] = []
        session.commit.side_effect = lambda: order.append("commit")
        alert = MagicMock(spec=PendingAlert)
        check = AsyncMock(return_value=BudgetCheck(budgets=2, alerts=[alert]))

        def notify(alerts):
            order.append("notify")
            assert list(alerts) == [alert]
            return 3

        with (
            patch("specivo.tasks.ai_usage.beat_lock", _held_lock),
            patch("specivo.tasks._async.task_session", fake_session),
            patch("specivo.services.ai_spend_budget_service.AiSpendBudgetService.check", check),
            patch("specivo.services.ai_spend_budget_service.AiSpendBudgetService.notify", side_effect=notify),
            caplog.at_level("INFO", logger="specivo.tasks.ai_usage"),
        ):
            sent = await check_ai_spend_budgets_async(today=date(2026, 3, 31))

        assert sent == 1
        check.assert_awaited_once_with(session, today=date(2026, 3, 31))
        assert order == ["commit", "notify"]
        assert "2 budgets, 1 alerts, 3 mails queued" in caplog.text

    async def test_skips_while_another_run_holds_the_lock(self):
        from specivo.tasks.ai_usage import check_ai_spend_budgets_async

        check = AsyncMock()
        with (
            patch("specivo.tasks.ai_usage.beat_lock", _lock_held_elsewhere),
            patch("specivo.services.ai_spend_budget_service.AiSpendBudgetService.check", check),
        ):
            assert await check_ai_spend_budgets_async() == 0
        check.assert_not_awaited()

    def test_a_failed_run_is_retried(self):
        from specivo.tasks.ai_usage import check_ai_spend_budgets

        def failing(coro):
            coro.close()
            raise RuntimeError("db down")

        with (
            patch("specivo.tasks.ai_usage.run_async", side_effect=failing),
            patch.object(check_ai_spend_budgets, "retry", side_effect=RuntimeError("retry")) as retry,
            pytest.raises(RuntimeError, match="retry"),
        ):
            check_ai_spend_budgets.run()
        retry.assert_called_once()
