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
from zoneinfo import available_timezones

from pydantic import BaseModel, Field, field_validator

_FREQ = Literal["daily", "weekly", "monthly", "yearly"]
_ANCHOR = Literal["fixed", "flexible"]
_BASE_STRATEGY = Literal["scheduled", "completion"]
_WORKING_DAY_ADJUSTMENT = Literal["none", "nearest", "next", "previous"]

# Cache the IANA timezone set once (the call walks the tz database on disk).
_AVAILABLE_TIMEZONES = available_timezones()


def _validate_timezone(value: str) -> str:
    """Reject non-IANA timezone names early (mirrors the engine's ZoneInfo use)."""
    if value not in _AVAILABLE_TIMEZONES:
        raise ValueError(f"Unknown IANA timezone: {value!r}")
    return value


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

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        return _validate_timezone(value)


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

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_timezone(value)


class RecurringPatternOut(BaseModel):
    """Full representation of a recurring pattern.

    ``project_key`` is derived in the router (mirroring ``VersionOut``); every
    other field maps 1:1 to an ORM column, so the response builder constructs
    this with ``model_validate(pattern)`` after setting ``project_key``.
    """

    model_config = {"from_attributes": True}

    id: int
    project_key: str
    name: str
    enabled: bool

    anchor_mode: str
    base_date_strategy: str

    # --- Recurrence rule ---
    freq: str
    rrule_interval: int
    byday: list[str] | None
    bymonthday: list[int] | None
    bymonth: list[int] | None
    bysetpos: list[int] | None
    rrule_count: int | None
    until: datetime | None
    rrule_raw: str | None
    dtstart: datetime
    timezone: str

    # --- Working-day handling ---
    working_day_adjustment: str
    working_days: list[int]
    holiday_calendar: list[str] | None
    creation_lead_time_days: int

    # --- Issue template ---
    template_tracker_id: int
    template_status_id: int | None
    template_priority_id: int | None
    template_category_id: int | None
    template_assigned_to_id: int | None
    template_fixed_version_id: int | None
    template_sprint_id: int | None
    template_subject: str
    template_description: str | None
    template_estimated_hours: Decimal | None
    template_metadata: dict
    is_private: bool

    # --- Carry-over / reset / rotation ---
    carry_over: dict
    reset_checklist: bool
    assignee_rotation: dict | None
    rotation_index: int
    start_offset_days: int | None
    due_offset_days: int | None

    # --- Bookkeeping ---
    last_run_at: datetime | None
    last_generated_occurrence_at: datetime | None
    created_at: datetime
    updated_at: datetime
    lock_version: int


class RecurrenceExceptionOut(BaseModel):
    """A skip / override exception attached to a recurring pattern."""

    model_config = {"from_attributes": True}

    id: int
    occurrence_at: datetime
    kind: str
    override_payload: dict | None
    materialized_issue_id: int | None


class OccurrencePreview(BaseModel):
    """A DB-free preview of upcoming occurrences (tz-aware UTC instants)."""

    occurrences: list[datetime]
    count: int


class SkipOccurrenceRequest(BaseModel):
    """Body for the skip endpoint — the scheduled occurrence to drop (EXDATE)."""

    occurrence_at: datetime


class OverrideOccurrenceRequest(BaseModel):
    """Body for the override endpoint — per-occurrence field overrides."""

    occurrence_at: datetime
    payload: dict = Field(default_factory=dict)


class SplitFromRequest(BaseModel):
    """Body for the this-and-future split endpoint.

    ``occurrence_at`` is the boundary: the old series is terminated just before
    it and ``new_pattern`` becomes a fresh series anchored at the boundary. The
    service overrides ``new_pattern.dtstart`` with ``occurrence_at``, so the
    ``dtstart`` supplied here is a placeholder (kept required to reuse the
    ``RecurringPatternCreate`` shape ``split_from`` expects).
    """

    occurrence_at: datetime
    new_pattern: RecurringPatternCreate
