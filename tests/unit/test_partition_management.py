"""Unit tests for the audit partition helpers and their scheduling.

The SQL itself runs against PostgreSQL in ``tests/integration/test_audit_partitions.py``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from specivo.tasks import cleanup
from specivo.tasks.partition_management import _add_months, _partition_name

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("day", "months", "expected"),
    [
        (date(2026, 3, 22), 0, date(2026, 3, 1)),
        (date(2026, 3, 22), 1, date(2026, 4, 1)),
        (date(2026, 11, 30), 3, date(2027, 2, 1)),
        (date(2026, 12, 1), 1, date(2027, 1, 1)),
        (date(2026, 1, 31), -1, date(2025, 12, 1)),
        (date(2026, 11, 4), -12, date(2025, 11, 1)),
    ],
)
def test_add_months_lands_on_the_first_of_the_month(day: date, months: int, expected: date) -> None:
    assert _add_months(day, months) == expected


def test_partition_name() -> None:
    assert _partition_name(date(2027, 1, 1)) == "security_audit_logs_2027_01"


def test_daily_task_creates_and_prunes_with_the_retention_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    import specivo.tasks.partition_management as pm
    from specivo.core.config import get_settings

    calls: list[tuple[str, Any]] = []

    async def ensure(session: Any, months_ahead: int = 3, **_: Any) -> list[str]:
        calls.append(("ensure", months_ahead))
        return []

    async def prune(session: Any, months: int, **_: Any) -> list[str]:
        calls.append(("prune", months))
        return []

    class _Session:
        async def commit(self) -> None:
            calls.append(("commit", None))

    class _Factory:
        async def __aenter__(self) -> _Session:
            return _Session()

        async def __aexit__(self, *exc: object) -> None:
            return None

    import specivo.tasks._async as async_module

    monkeypatch.setattr(pm, "ensure_partitions", ensure)
    monkeypatch.setattr(pm, "prune_partitions", prune)
    monkeypatch.setattr(async_module, "task_session", lambda: _Factory())
    monkeypatch.setattr(get_settings(), "audit_log_retention_months", 7)

    cleanup.run_async(cleanup._ensure_audit_partitions_async())

    assert calls == [("ensure", 3), ("prune", 7), ("commit", None)]


def test_partitions_are_checked_when_a_worker_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    from celery.signals import worker_ready

    receivers = [
        ref() if callable(ref) and not hasattr(ref, "__name__") else ref for _key, ref in worker_ready.receivers
    ]
    assert cleanup._ensure_audit_partitions_at_start in receivers

    ran: list[str] = []

    async def fake() -> None:
        ran.append("checked")

    monkeypatch.setattr(cleanup, "_ensure_audit_partitions_async", fake)
    cleanup._ensure_audit_partitions_at_start(sender=None)

    assert ran == ["checked"]


def test_a_failing_check_at_worker_start_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken() -> None:
        raise RuntimeError("database is down")

    monkeypatch.setattr(cleanup, "_ensure_audit_partitions_async", broken)
    cleanup._ensure_audit_partitions_at_start()
