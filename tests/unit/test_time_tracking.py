"""Unit tests for time tracking logic.

Tests timer calculation, overnight protection, and schema validation.
No database required — pure Python logic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError


class TestTimerElapsedHours:
    """Test timer elapsed-hours calculation logic."""

    def test_timer_calculates_elapsed_hours(self) -> None:
        """Start at T, now at T+90min -> 1.5 hours."""
        from specivo.services.time_entry_service import _compute_elapsed_hours

        started_at = datetime(2026, 3, 22, 10, 0, 0, tzinfo=UTC)
        now = started_at + timedelta(minutes=90)
        hours = _compute_elapsed_hours(started_at, now)
        assert hours == Decimal("1.50")

    def test_timer_overnight_protection(self) -> None:
        """Elapsed > 12h -> capped at 12.0."""
        from specivo.services.time_entry_service import _compute_elapsed_hours

        started_at = datetime(2026, 3, 22, 10, 0, 0, tzinfo=UTC)
        now = started_at + timedelta(hours=15)
        hours = _compute_elapsed_hours(started_at, now)
        assert hours == Decimal("12.00")

    def test_time_entry_hours_precision(self) -> None:
        """Decimal('1.25') stays precise, no float drift."""
        from specivo.services.time_entry_service import _compute_elapsed_hours

        started_at = datetime(2026, 3, 22, 10, 0, 0, tzinfo=UTC)
        now = started_at + timedelta(hours=1, minutes=15)
        hours = _compute_elapsed_hours(started_at, now)
        assert hours == Decimal("1.25")
        assert isinstance(hours, Decimal)


class TestTimeEntrySchemaValidation:
    """Test Pydantic schema validation for time entry creation."""

    def test_pydantic_schema_rejects_zero_hours(self) -> None:
        """TimeEntryCreate with hours=0 -> validation error."""
        from specivo.schemas.time_entry import TimeEntryCreate

        with pytest.raises(ValidationError):
            TimeEntryCreate(
                activity_id=1,
                hours=Decimal("0"),
                spent_on="2026-03-22",
            )

    def test_pydantic_schema_rejects_negative_hours(self) -> None:
        """TimeEntryCreate with hours=-1 -> validation error."""
        from specivo.schemas.time_entry import TimeEntryCreate

        with pytest.raises(ValidationError):
            TimeEntryCreate(
                activity_id=1,
                hours=Decimal("-1"),
                spent_on="2026-03-22",
            )

    def test_pydantic_schema_accepts_valid_hours(self) -> None:
        """TimeEntryCreate with hours=2.5 -> OK."""
        from specivo.schemas.time_entry import TimeEntryCreate

        entry = TimeEntryCreate(
            activity_id=1,
            hours=Decimal("2.50"),
            spent_on="2026-03-22",
        )
        assert entry.hours == Decimal("2.50")

    def test_timer_start_requires_project_id(self) -> None:
        """TimerStartRequest requires project_id."""
        from specivo.schemas.time_entry import TimerStartRequest

        with pytest.raises(ValidationError):
            TimerStartRequest()  # type: ignore[call-arg]

    def test_timer_start_accepts_valid_request(self) -> None:
        """TimerStartRequest with project_id -> OK."""
        from specivo.schemas.time_entry import TimerStartRequest

        req = TimerStartRequest(project_id=1)
        assert req.project_id == 1
        assert req.issue_id is None
