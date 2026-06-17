"""Pydantic schemas for RecurringPattern create/update.

These mirror the ``RecurringPattern`` ORM columns. They are deliberately
permissive about the recurrence rule itself: rule *coherence* is validated in
:class:`~specivo.services.recurring_pattern_service.RecurringPatternService` by
building a :class:`~specivo.services.recurrence.RecurrenceSpec` and expanding a
tiny window (the engine raises ``ValueError`` on an incoherent rule, which the
service converts into a ``ValidationError``).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

_FREQ = Literal["daily", "weekly", "monthly", "yearly"]
_ANCHOR = Literal["fixed", "flexible"]
_BASE_STRATEGY = Literal["scheduled", "completion"]
_WORKING_DAY_ADJUSTMENT = Literal["none", "nearest", "next", "previous"]


class RecurringPatternCreate(BaseModel):
    """Payload for creating a recurring pattern.

    ``project`` and ``author`` are passed separately to the service (they are
    not part of this payload). Everything else maps 1:1 to ORM columns.
    """

    name: str = Field(min_length=1, max_length=255)
    enabled: bool = True

    # --- Issue template ---
    template_tracker_id: int
    template_status_id: int | None = None
    template_priority_id: int | None = None
    template_category_id: int | None = None
    template_assigned_to_id: int | None = None
    template_fixed_version_id: int | None = None
    template_sprint_id: int | None = None
    template_subject: str = Field(min_length=1, max_length=1024)
    template_description: str | None = None
    template_estimated_hours: Decimal | None = Field(default=None, ge=0)
    template_metadata: dict = Field(default_factory=dict)
    is_private: bool = False

    # --- Recurrence rule (RFC 5545 subset) ---
    freq: _FREQ
    rrule_interval: int = Field(default=1, ge=1)
    byday: list[str] | None = None
    bymonthday: list[int] | None = None
    bymonth: list[int] | None = None
    bysetpos: list[int] | None = None
    rrule_count: int | None = Field(default=None, ge=1)
    until: datetime | None = None
    rrule_raw: str | None = None

    # --- Tracker-style extensions ---
    anchor_mode: _ANCHOR = "fixed"
    base_date_strategy: _BASE_STRATEGY = "scheduled"
    dtstart: datetime
    timezone: str = "UTC"
    working_day_adjustment: _WORKING_DAY_ADJUSTMENT = "none"
    working_days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    holiday_calendar: list[str] | None = None
    creation_lead_time_days: int = Field(default=30, ge=1)

    # --- Carry-over / reset / rotation ---
    carry_over: dict = Field(default_factory=dict)
    reset_checklist: bool = True
    assignee_rotation: dict | None = None
    start_offset_days: int | None = None
    due_offset_days: int | None = None


class RecurringPatternUpdate(BaseModel):
    """Partial update for a recurring pattern (PATCH semantics).

    Every field is optional. ``None`` means "leave unchanged" for scalar fields;
    to clear a nullable field, callers should use the dedicated edit-scope
    methods or set it explicitly through a future field-mask. For 0.2.0 a plain
    update touches only the fields that are provided (non-None).
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = None

    template_tracker_id: int | None = None
    template_status_id: int | None = None
    template_priority_id: int | None = None
    template_category_id: int | None = None
    template_assigned_to_id: int | None = None
    template_fixed_version_id: int | None = None
    template_sprint_id: int | None = None
    template_subject: str | None = Field(default=None, min_length=1, max_length=1024)
    template_description: str | None = None
    template_estimated_hours: Decimal | None = Field(default=None, ge=0)
    template_metadata: dict | None = None
    is_private: bool | None = None

    freq: _FREQ | None = None
    rrule_interval: int | None = Field(default=None, ge=1)
    byday: list[str] | None = None
    bymonthday: list[int] | None = None
    bymonth: list[int] | None = None
    bysetpos: list[int] | None = None
    rrule_count: int | None = Field(default=None, ge=1)
    until: datetime | None = None
    rrule_raw: str | None = None

    anchor_mode: _ANCHOR | None = None
    base_date_strategy: _BASE_STRATEGY | None = None
    dtstart: datetime | None = None
    timezone: str | None = None
    working_day_adjustment: _WORKING_DAY_ADJUSTMENT | None = None
    working_days: list[int] | None = None
    holiday_calendar: list[str] | None = None
    creation_lead_time_days: int | None = Field(default=None, ge=1)

    carry_over: dict | None = None
    reset_checklist: bool | None = None
    assignee_rotation: dict | None = None
    start_offset_days: int | None = None
    due_offset_days: int | None = None
