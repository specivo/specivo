"""factory_boy factories for lookup models: Tracker, IssueStatus, IssuePriority."""

from __future__ import annotations

import factory

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker


class StatusFactory(factory.Factory):
    """Builds IssueStatus instances.

    Usage::

        status = StatusFactory.build(name="Open")
    """

    class Meta:
        model = IssueStatus

    name = factory.Sequence(lambda n: f"Status {n}")
    category = "backlog"
    position = factory.Sequence(lambda n: n + 1)
    default_done_ratio = None


class ClosedStatusFactory(StatusFactory):
    """A StatusFactory variant that produces terminal (closed) statuses."""

    category = "closed"
    name = factory.Sequence(lambda n: f"Closed Status {n}")


class DoneStatusFactory(StatusFactory):
    """A StatusFactory variant that produces done (completed) statuses."""

    category = "done"
    name = factory.Sequence(lambda n: f"Done Status {n}")


class TrackerFactory(factory.Factory):
    """Builds Tracker instances.

    Usage::

        tracker = TrackerFactory.build(name="Bug")
    """

    class Meta:
        model = Tracker

    name = factory.Sequence(lambda n: f"Tracker {n}")
    default_status_id = None
    is_in_roadmap = True
    position = factory.Sequence(lambda n: n + 1)
    description = None
    disabled_core_fields = factory.LazyFunction(list)


class PriorityFactory(factory.Factory):
    """Builds IssuePriority instances.

    Usage::

        priority = PriorityFactory.build(name="High")
        default_priority = PriorityFactory.build(name="Normal", is_default=True)
    """

    class Meta:
        model = IssuePriority

    name = factory.Sequence(lambda n: f"Priority {n}")
    position = factory.Sequence(lambda n: n + 1)
    is_default = False
    active = True
