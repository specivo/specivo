"""factory_boy factory for the RecurringPattern model."""

from __future__ import annotations

from datetime import UTC, datetime

import factory

from specivo.models.recurring_pattern import RecurringPattern


class RecurringPatternFactory(factory.Factory):
    """Builds RecurringPattern model instances.

    FK fields (``project_id``, ``author_id``, ``template_tracker_id``) must be
    overridden with real IDs from fixtures.

    Usage::

        pattern = RecurringPatternFactory.build(
            project_id=project.id,
            author_id=user.id,
            template_tracker_id=tracker.id,
            template_status_id=status.id,
            freq="daily",
            dtstart=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        )
    """

    class Meta:
        model = RecurringPattern

    project_id = 1
    author_id = 1
    name = factory.Sequence(lambda n: f"Pattern {n}")
    enabled = True

    # Template
    template_tracker_id = 1
    template_status_id = None
    template_priority_id = None
    template_category_id = None
    template_assigned_to_id = None
    template_fixed_version_id = None
    template_sprint_id = None
    template_subject = factory.Sequence(lambda n: f"Recurring task {n}")
    template_description = None
    template_estimated_hours = None
    template_metadata = factory.LazyFunction(dict)
    is_private = False

    # Recurrence rule
    freq = "daily"
    rrule_interval = 1
    byday = None
    bymonthday = None
    bymonth = None
    bysetpos = None
    rrule_count = None
    until = None
    rrule_raw = None

    # Extensions
    anchor_mode = "fixed"
    base_date_strategy = "scheduled"
    dtstart = factory.LazyFunction(lambda: datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
    timezone = "UTC"
    working_day_adjustment = "none"
    working_days = factory.LazyFunction(lambda: [1, 2, 3, 4, 5])
    holiday_calendar = None
    creation_lead_time_days = 30

    # Carry-over / reset / rotation
    carry_over = factory.LazyFunction(dict)
    reset_checklist = True
    assignee_rotation = None
    rotation_index = 0
    start_offset_days = None
    due_offset_days = None
