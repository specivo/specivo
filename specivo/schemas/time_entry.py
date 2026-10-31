"""Pydantic schemas for time tracking: time entries, activities, timers."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from specivo.schemas.common import IdName, PaginatedResponse


class TimeEntryCreate(BaseModel):
    """Payload for creating a time entry."""

    issue_id: int | None = None
    activity_id: int
    hours: Decimal = Field(gt=0, max_digits=8, decimal_places=2)
    comments: str | None = None
    spent_on: date
    is_billable: bool = False


class TimeEntryUpdate(BaseModel):
    """Payload for partial update of a time entry (PATCH semantics)."""

    activity_id: int | None = None
    hours: Decimal | None = Field(None, gt=0, max_digits=8, decimal_places=2)
    comments: str | None = None
    spent_on: date | None = None
    is_billable: bool | None = None


class TimeEntryOut(BaseModel):
    """Time entry representation returned by the API."""

    model_config = {"from_attributes": True}

    id: int
    project_id: int
    issue_id: int | None
    user: IdName
    activity: IdName
    hours: Decimal
    comments: str | None
    spent_on: date
    is_billable: bool
    created_at: datetime
    updated_at: datetime


class TimeEntryListResponse(PaginatedResponse[TimeEntryOut]):
    """Paginated time entry list response."""

    pass


class TimerStartRequest(BaseModel):
    """Payload for starting a timer."""

    project_id: int
    issue_id: int | None = None
    comments: str | None = None


class TimerStopRequest(BaseModel):
    """Payload for stopping a timer.

    ``comments`` replace the comment the timer was started with, when given.
    """

    activity_id: int
    comments: str | None = None


class TimerOut(BaseModel):
    """Timer representation returned by the API."""

    model_config = {"from_attributes": True}

    id: int
    user_id: int
    project_id: int
    issue_id: int | None
    started_at: datetime
    comments: str | None
    elapsed_seconds: int


class ReplacedTimerOut(BaseModel):
    """What became of the timer that was running when another one was started.

    ``outcome`` is ``logged`` when its time was logged (with the default
    activity) as entry ``entry_id`` of ``hours``, or ``discarded`` when the
    user may no longer log time where it ran, so nothing was logged.
    """

    outcome: Literal["logged", "discarded"]
    project_id: int
    project_key: str
    issue_id: int | None
    issue_key: str | None
    hours: Decimal | None
    entry_id: int | None


class TimerStartOut(TimerOut):
    """The started timer, and the one it replaced (``None`` when no timer was running)."""

    replaced: ReplacedTimerOut | None = None


class ActivityOut(BaseModel):
    """Activity representation returned by the API."""

    model_config = {"from_attributes": True}

    id: int
    name: str
    position: int
    is_default: bool
    active: bool
