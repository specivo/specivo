"""factory_boy factory for the Issue model."""

from __future__ import annotations

import factory

from specivo.models.issue import Issue


class IssueFactory(factory.Factory):
    """Builds Issue model instances.

    All FK fields (project_id, tracker_id, status_id, priority_id, author_id)
    must be overridden with real IDs from fixtures — they have no defaults
    because they reference other tables.

    Usage::

        issue = IssueFactory.build(
            project_id=project.id,
            project_key=project.key,
            sequence_number=1,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
            author_id=user.id,
            subject="Implement login page",
        )
    """

    class Meta:
        model = Issue

    # Identity — must be set from real project data in tests
    project_id = 1
    project_key = "TEST"
    sequence_number = factory.Sequence(lambda n: n + 1)

    # Classification — must be overridden with seeded lookup IDs
    tracker_id = 1
    status_id = 1
    priority_id = 1
    category_id = None

    # People
    author_id = 1
    assigned_to_id = None

    # Content
    subject = factory.Sequence(lambda n: f"Test Issue {n}")
    description = None
    metadata = factory.LazyFunction(dict)

    # Nested set defaults (leaf node)
    parent_id = None
    root_id = None
    lft = 1
    rgt = 2

    # Planning
    fixed_version_id = None
    done_ratio = 0
    estimated_hours = None
    original_estimate = None
    remaining_estimate = None
    start_date = None
    due_date = None
    closed_on = None
    is_private = False

    # Optimistic locking
    lock_version = 0
