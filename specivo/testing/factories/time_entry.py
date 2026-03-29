"""factory_boy factories for time tracking models."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import factory

from specivo.models.time_entry import ActiveTimer, TimeEntry, TimeEntryActivity


class TimeEntryActivityFactory(factory.Factory):
    """Builds TimeEntryActivity instances."""

    class Meta:
        model = TimeEntryActivity

    name = factory.Sequence(lambda n: f"Activity {n}")
    position = factory.Sequence(lambda n: n + 1)
    is_default = False
    active = True


class TimeEntryFactory(factory.Factory):
    """Builds TimeEntry instances.

    FK fields (project_id, user_id, activity_id) must be overridden with real IDs.
    """

    class Meta:
        model = TimeEntry

    project_id = 1
    issue_id = None
    user_id = 1
    activity_id = 1
    hours = Decimal("1.00")
    comments = None
    spent_on = factory.LazyFunction(date.today)
    is_billable = False


class ActiveTimerFactory(factory.Factory):
    """Builds ActiveTimer instances."""

    class Meta:
        model = ActiveTimer

    user_id = 1
    issue_id = None
    project_id = 1
    started_at = factory.LazyFunction(lambda: datetime.now(UTC))
    comments = None
