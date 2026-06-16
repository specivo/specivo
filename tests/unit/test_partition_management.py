"""Unit tests for partition auto-creation.

Verifies that the partition management task:
1. Creates partitions 3 months ahead for security_audit_logs
2. Is idempotent — running twice does not error (IF NOT EXISTS)

The function under test does not exist yet. These tests import from
``app.tasks.partition_management`` which will be created in the green phase.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

# This import will fail in red phase — the module does not exist yet.
# That is the expected TDD behavior.
from specivo.tasks.partition_management import ensure_partitions

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_partitions_creates_future_months():
    """The ensure_partitions task should create partitions for 3 months ahead.

    It should create partitions for 3 months ahead, e.g.:
    - 2026-04 (April)
    - 2026-05 (May)
    - 2026-06 (June)

    Each partition should cover a full calendar month with the naming
    convention: security_audit_logs_YYYY_MM
    """
    mock_session = AsyncMock()
    mock_execute = AsyncMock()
    mock_session.execute = mock_execute

    # Patch today's date to make the test deterministic
    fixed_today = date(2026, 3, 22)
    with patch("specivo.tasks.partition_management.date") as mock_date:
        mock_date.today.return_value = fixed_today
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)

        await ensure_partitions(session=mock_session)

    # Verify that execute was called for each of the 3 future months
    assert mock_execute.call_count >= 3, f"Expected at least 3 partition creation calls, got {mock_execute.call_count}"

    # Extract the SQL statements from calls
    sql_strings = []
    for c in mock_execute.call_args_list:
        sql_arg = str(c.args[0]) if c.args else ""
        sql_strings.append(sql_arg)

    # Verify partition names follow the expected convention
    expected_partitions = [
        "security_audit_logs_2026_04",
        "security_audit_logs_2026_05",
        "security_audit_logs_2026_06",
    ]
    for partition_name in expected_partitions:
        found = any(partition_name in sql for sql in sql_strings)
        assert found, (
            f"Expected partition '{partition_name}' to be created, but it was not found in executed SQL: {sql_strings}"
        )


@pytest.mark.asyncio
async def test_ensure_partitions_includes_current_month():
    """The current month's partition must be created, not just future months.

    On a fresh deploy mid-month with no existing partitions, audit inserts
    must have a partition to land in *immediately*. If the task only ever
    creates next-month and beyond, current-month inserts route to the
    default partition until the next monthly tick — which is a silent
    data-routing bug.

    Concretely, with months_ahead=3 and today = 2026-03-22, the task must
    create partitions for 2026-03 (current), 2026-04, 2026-05, and 2026-06
    — four total, including the current month.
    """
    mock_session = AsyncMock()
    mock_execute = AsyncMock()
    mock_session.execute = mock_execute

    fixed_today = date(2026, 3, 22)
    with patch("specivo.tasks.partition_management.date") as mock_date:
        mock_date.today.return_value = fixed_today
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)

        await ensure_partitions(session=mock_session, months_ahead=3)

    sql_strings = [str(c.args[0]) if c.args else "" for c in mock_execute.call_args_list]

    # Current month MUST be present.
    assert any("security_audit_logs_2026_03" in sql for sql in sql_strings), (
        "Current month partition (security_audit_logs_2026_03) was not created. "
        f"Executed SQL: {sql_strings}"
    )

    # And the next three months must also be present.
    for partition_name in (
        "security_audit_logs_2026_04",
        "security_audit_logs_2026_05",
        "security_audit_logs_2026_06",
    ):
        assert any(partition_name in sql for sql in sql_strings), (
            f"Expected partition '{partition_name}' to be created. "
            f"Executed SQL: {sql_strings}"
        )

    # And there should be exactly 4 partition CREATE statements
    # (current + 3 future) — no more, no less.
    partition_creates = [s for s in sql_strings if "PARTITION OF security_audit_logs" in s]
    assert len(partition_creates) == 4, (
        f"Expected exactly 4 partition CREATE statements (current + 3 ahead), "
        f"got {len(partition_creates)}: {partition_creates}"
    )


@pytest.mark.asyncio
async def test_ensure_partitions_current_month_at_year_boundary():
    """Year rollover must work for the current-month partition too.

    With today = 2026-12-15 and months_ahead=3, partitions must be created
    for 2026-12 (current), 2027-01, 2027-02, 2027-03.
    """
    mock_session = AsyncMock()
    mock_execute = AsyncMock()
    mock_session.execute = mock_execute

    fixed_today = date(2026, 12, 15)
    with patch("specivo.tasks.partition_management.date") as mock_date:
        mock_date.today.return_value = fixed_today
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)

        await ensure_partitions(session=mock_session, months_ahead=3)

    sql_strings = [str(c.args[0]) if c.args else "" for c in mock_execute.call_args_list]

    for partition_name in (
        "security_audit_logs_2026_12",
        "security_audit_logs_2027_01",
        "security_audit_logs_2027_02",
        "security_audit_logs_2027_03",
    ):
        assert any(partition_name in sql for sql in sql_strings), (
            f"Expected partition '{partition_name}' to be created. "
            f"Executed SQL: {sql_strings}"
        )


@pytest.mark.asyncio
async def test_ensure_partitions_is_idempotent():
    """Running ensure_partitions twice should not error.

    The SQL should use IF NOT EXISTS (or equivalent) so that creating
    an already-existing partition is a no-op.
    """
    mock_session = AsyncMock()
    mock_execute = AsyncMock()
    mock_session.execute = mock_execute

    fixed_today = date(2026, 3, 22)
    with patch("specivo.tasks.partition_management.date") as mock_date:
        mock_date.today.return_value = fixed_today
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)

        # First run — creates partitions
        await ensure_partitions(session=mock_session)

        # Second run — should not raise
        await ensure_partitions(session=mock_session)

    # Verify that the SQL uses IF NOT EXISTS
    sql_strings = []
    for c in mock_execute.call_args_list:
        sql_arg = str(c.args[0]) if c.args else ""
        sql_strings.append(sql_arg)

    partition_sqls = [s for s in sql_strings if "security_audit_logs_" in s]
    assert len(partition_sqls) > 0, "Expected partition creation SQL statements"

    for sql in partition_sqls:
        assert "IF NOT EXISTS" in sql.upper() or "if not exists" in sql.lower(), (
            f"Partition creation SQL should use IF NOT EXISTS for idempotency: {sql}"
        )
