"""Seed command: populate default roles, trackers, statuses, and priorities.

Usage::

    uv run python -m specivo.cli.seed

The command is idempotent — it upserts by name so it is safe to re-run after
migrations without producing duplicates.

Default roles
-------------
Manager   - all permissions via ["*"] wildcard
Developer - core development permissions
Reporter  - read + create + comment
Agent     - API-facing role for automated agents (no management/delete rights)

Default statuses
----------------
New (backlog), In Progress (active), Resolved (done), Feedback (active),
Closed (closed), Rejected (closed)

Default trackers
----------------
Bug, Feature, Task, Support (all default to "New" status)

Default priorities
------------------
Low, Normal (is_default), High, Urgent, Immediate
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from specivo.core.config import get_settings
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.metadata_preset import MetadataPreset
from specivo.models.role import Role
from specivo.models.search import EmbeddingModel
from specivo.models.time_entry import TimeEntryActivity
from specivo.models.workflow import WorkflowTransition

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Role definitions
# ---------------------------------------------------------------------------

_DEFAULT_ROLES: list[dict] = [
    {
        "name": "Manager",
        "position": 1,
        "assignable": True,
        "builtin": 0,
        # Wildcard grants every permission now and in the future
        "permissions": ["*"],
        "issues_visibility": "all",
        "settings": {},
    },
    {
        "name": "Developer",
        "position": 2,
        "assignable": True,
        "builtin": 0,
        "permissions": [
            "add_issues",
            "edit_issues",
            "add_issue_notes",
            "edit_own_notes",
            "manage_issue_relations",
            "manage_subtasks",
            "view_issues",
            "view_wiki",
            "log_time",
            "view_time_entries",
        ],
        "issues_visibility": "default",
        "settings": {},
    },
    {
        "name": "Reporter",
        "position": 3,
        "assignable": True,
        "builtin": 0,
        "permissions": [
            "add_issues",
            "add_issue_notes",
            "view_issues",
            "view_wiki",
        ],
        "issues_visibility": "default",
        "settings": {},
    },
    {
        "name": "Agent",
        "position": 4,
        "assignable": True,
        "builtin": 0,
        # API-facing service account role: full issue lifecycle but no management
        "permissions": [
            "add_issues",
            "edit_issues",
            "add_issue_notes",
            "edit_own_notes",
            "manage_issue_relations",
            "view_issues",
            "view_wiki",
            "log_time",
        ],
        "issues_visibility": "default",
        "settings": {},
    },
]


# ---------------------------------------------------------------------------
# Status / Tracker / Priority definitions
# ---------------------------------------------------------------------------

_DEFAULT_STATUSES: list[dict] = [
    {"name": "New", "position": 1, "category": "backlog", "default_done_ratio": None},
    {"name": "In Progress", "position": 2, "category": "active", "default_done_ratio": None},
    {"name": "Resolved", "position": 3, "category": "done", "default_done_ratio": 100},
    {"name": "Feedback", "position": 4, "category": "active", "default_done_ratio": None},
    {"name": "Closed", "position": 5, "category": "closed", "default_done_ratio": 100},
    {"name": "Rejected", "position": 6, "category": "closed", "default_done_ratio": None},
]

# Tracker definitions — default_status_id resolved after statuses are seeded
_DEFAULT_TRACKERS: list[dict] = [
    {"name": "Bug", "position": 1, "is_in_roadmap": True, "description": None, "disabled_core_fields": []},
    {"name": "Feature", "position": 2, "is_in_roadmap": True, "description": None, "disabled_core_fields": []},
    {"name": "Task", "position": 3, "is_in_roadmap": True, "description": None, "disabled_core_fields": []},
    {"name": "Support", "position": 4, "is_in_roadmap": False, "description": None, "disabled_core_fields": []},
]

# Priority definitions
_DEFAULT_PRIORITIES: list[dict] = [
    {"name": "Low", "position": 1, "is_default": False, "active": True},
    {"name": "Normal", "position": 2, "is_default": True, "active": True},
    {"name": "High", "position": 3, "is_default": False, "active": True},
    {"name": "Urgent", "position": 4, "is_default": False, "active": True},
    {"name": "Immediate", "position": 5, "is_default": False, "active": True},
]


_DEFAULT_TIME_ENTRY_ACTIVITIES: list[dict] = [
    {"name": "Development", "position": 1, "is_default": True, "active": True},
    {"name": "Design", "position": 2, "is_default": False, "active": True},
    {"name": "Testing", "position": 3, "is_default": False, "active": True},
    {"name": "Meetings", "position": 4, "is_default": False, "active": True},
    {"name": "Support", "position": 5, "is_default": False, "active": True},
]


async def seed_time_entry_activities(session: AsyncSession) -> None:
    """Upsert default time entry activities."""
    for data in _DEFAULT_TIME_ENTRY_ACTIVITIES:
        result = await session.execute(select(TimeEntryActivity).where(TimeEntryActivity.name == data["name"]))
        activity = result.scalar_one_or_none()
        if activity is None:
            activity = TimeEntryActivity(**data)
            session.add(activity)
            logger.info("Created activity: %s", data["name"])
        else:
            for key, value in data.items():
                setattr(activity, key, value)
            logger.info("Updated activity: %s", data["name"])

    await session.flush()
    print(
        f"Seeded {len(_DEFAULT_TIME_ENTRY_ACTIVITIES)} activities: "
        f"{[a['name'] for a in _DEFAULT_TIME_ENTRY_ACTIVITIES]}"
    )


async def seed_statuses(session: AsyncSession) -> None:
    """Upsert default issue statuses."""
    for data in _DEFAULT_STATUSES:
        result = await session.execute(select(IssueStatus).where(IssueStatus.name == data["name"]))
        status = result.scalar_one_or_none()
        if status is None:
            status = IssueStatus(**data)
            session.add(status)
            logger.info("Created status: %s", data["name"])
        else:
            for key, value in data.items():
                setattr(status, key, value)
            logger.info("Updated status: %s", data["name"])

    await session.flush()
    print(f"Seeded {len(_DEFAULT_STATUSES)} statuses: {[s['name'] for s in _DEFAULT_STATUSES]}")


async def seed_trackers(session: AsyncSession) -> None:
    """Upsert default trackers, pointing default_status_id at 'New'."""
    # Resolve the "New" status id
    result = await session.execute(select(IssueStatus).where(IssueStatus.name == "New"))
    new_status = result.scalar_one_or_none()
    new_status_id = new_status.id if new_status else None

    for data in _DEFAULT_TRACKERS:
        result = await session.execute(select(Tracker).where(Tracker.name == data["name"]))
        tracker = result.scalar_one_or_none()
        tracker_data = {**data, "default_status_id": new_status_id}
        if tracker is None:
            tracker = Tracker(**tracker_data)
            session.add(tracker)
            logger.info("Created tracker: %s", data["name"])
        else:
            for key, value in tracker_data.items():
                setattr(tracker, key, value)
            logger.info("Updated tracker: %s", data["name"])

    await session.flush()
    print(f"Seeded {len(_DEFAULT_TRACKERS)} trackers: {[t['name'] for t in _DEFAULT_TRACKERS]}")


async def seed_priorities(session: AsyncSession) -> None:
    """Upsert default issue priorities."""
    for data in _DEFAULT_PRIORITIES:
        result = await session.execute(select(IssuePriority).where(IssuePriority.name == data["name"]))
        priority = result.scalar_one_or_none()
        if priority is None:
            priority = IssuePriority(**data)
            session.add(priority)
            logger.info("Created priority: %s", data["name"])
        else:
            for key, value in data.items():
                setattr(priority, key, value)
            logger.info("Updated priority: %s", data["name"])

    await session.flush()
    print(f"Seeded {len(_DEFAULT_PRIORITIES)} priorities: {[p['name'] for p in _DEFAULT_PRIORITIES]}")


async def seed_roles(session: AsyncSession) -> None:
    """Upsert default roles into the database.

    Existing roles with the same name are updated to match the current
    definition so that re-seeding after a spec change is safe.
    New roles are inserted.
    """
    for role_data in _DEFAULT_ROLES:
        result = await session.execute(select(Role).where(Role.name == role_data["name"]))
        role = result.scalar_one_or_none()

        if role is None:
            role = Role(**role_data)
            session.add(role)
            logger.info("Created role: %s", role_data["name"])
        else:
            # Update to latest definition (idempotent)
            for key, value in role_data.items():
                setattr(role, key, value)
            logger.info("Updated role: %s", role_data["name"])

    await session.commit()
    print(f"Seeded {len(_DEFAULT_ROLES)} roles: {[r['name'] for r in _DEFAULT_ROLES]}")


# ---------------------------------------------------------------------------
# Default workflow transitions
# ---------------------------------------------------------------------------

# (tracker_name, old_status_name, new_status_name)
_DEFAULT_TRANSITIONS: list[tuple[str, str, str]] = [
    # Bug workflow
    ("Bug", "New", "In Progress"),
    ("Bug", "New", "Rejected"),
    ("Bug", "In Progress", "Resolved"),
    ("Bug", "In Progress", "Feedback"),
    ("Bug", "Resolved", "Closed"),
    ("Bug", "Resolved", "In Progress"),
    ("Bug", "Feedback", "In Progress"),
    # Feature workflow
    ("Feature", "New", "In Progress"),
    ("Feature", "New", "Rejected"),
    ("Feature", "In Progress", "Resolved"),
    ("Feature", "In Progress", "Feedback"),
    ("Feature", "Resolved", "Closed"),
    ("Feature", "Resolved", "In Progress"),
    ("Feature", "Feedback", "In Progress"),
    # Task workflow
    ("Task", "New", "In Progress"),
    ("Task", "New", "Rejected"),
    ("Task", "In Progress", "Resolved"),
    ("Task", "In Progress", "Feedback"),
    ("Task", "Resolved", "Closed"),
    ("Task", "Resolved", "In Progress"),
    ("Task", "Feedback", "In Progress"),
]


async def seed_workflow_transitions(session: AsyncSession) -> None:
    """Upsert default workflow transitions for Manager and Developer roles."""
    # Build lookup maps
    status_result = await session.execute(select(IssueStatus))
    statuses = {s.name: s.id for s in status_result.scalars().all()}

    tracker_result = await session.execute(select(Tracker))
    trackers = {t.name: t.id for t in tracker_result.scalars().all()}

    role_result = await session.execute(select(Role))
    roles = {r.name: r.id for r in role_result.scalars().all()}

    target_roles = ["Manager", "Developer"]
    count = 0
    for tracker_name, old_name, new_name in _DEFAULT_TRANSITIONS:
        tracker_id = trackers.get(tracker_name)
        old_id = statuses.get(old_name)
        new_id = statuses.get(new_name)
        if tracker_id is None or old_id is None or new_id is None:
            continue

        for role_name in target_roles:
            role_id = roles.get(role_name)
            if role_id is None:
                continue

            # Check if already exists
            existing = await session.execute(
                select(WorkflowTransition).where(
                    WorkflowTransition.tracker_id == tracker_id,
                    WorkflowTransition.role_id == role_id,
                    WorkflowTransition.old_status_id == old_id,
                    WorkflowTransition.new_status_id == new_id,
                )
            )
            if existing.scalar_one_or_none() is None:
                session.add(
                    WorkflowTransition(
                        tracker_id=tracker_id,
                        role_id=role_id,
                        old_status_id=old_id,
                        new_status_id=new_id,
                    )
                )
                count += 1

    await session.flush()
    print(f"Seeded {count} new workflow transitions")


async def seed_embedding_model(session: AsyncSession) -> None:
    """Insert the default local embedding model if it does not already exist.

    Idempotent: does nothing when a model with the same name is already present,
    and never overwrites admin-modified values.
    """
    result = await session.execute(select(EmbeddingModel).where(EmbeddingModel.name == "multilingual-e5-small"))
    existing = result.scalar_one_or_none()
    if existing is not None:
        logger.info("Embedding model 'multilingual-e5-small' already exists, skipping")
        return

    model = EmbeddingModel(
        name="multilingual-e5-small",
        provider="local",
        model_name="multilingual-e5-small",
        dimensions=384,
        is_default=True,
    )
    session.add(model)
    await session.flush()
    logger.info("Seeded default embedding model: multilingual-e5-small")


async def seed_settings(session: AsyncSession) -> None:
    """Seed default application settings (insert only, never overwrite user values)."""
    import json

    from specivo.core.constants import DEFAULT_AVATAR_PALETTE
    from specivo.models.setting import Setting

    defaults = {
        "brand_name": "Specivo",
        "avatar_color_palette": json.dumps(DEFAULT_AVATAR_PALETTE),
    }
    for key, value in defaults.items():
        result = await session.execute(select(Setting).where(Setting.key == key))
        if result.scalar_one_or_none() is None:
            session.add(Setting(key=key, value=value))
            print(f"  Seeded setting: {key}={value}")

    await session.flush()


# ---------------------------------------------------------------------------
# Default metadata presets
# ---------------------------------------------------------------------------

_DEFAULT_METADATA_PRESETS: list[dict] = [
    {
        "slug": "software-development",
        "name": "Software Development",
        "description": "Track commits, branches, pull requests, and components for development workflows.",
        "icon": "code",
        "is_builtin": True,
        "schema_definition": {
            "type": "object",
            "properties": {
                "component": {
                    "type": "string",
                    "description": "Codebase component or module",
                },
                "commits": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^[a-f0-9]{7,40}$"},
                    "description": "Git commit hashes",
                },
                "branches": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Git branch names",
                },
                "pull_requests": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Pull/merge request URLs or identifiers",
                },
            },
        },
    },
    {
        "slug": "bug-triage",
        "name": "Bug Triage",
        "description": "Classify bugs by severity, environment, and reproduction steps for efficient triage.",
        "icon": "bug",
        "is_builtin": True,
        "schema_definition": {
            "type": "object",
            "properties": {
                "severity": {
                    "type": "string",
                    "enum": ["critical", "major", "minor", "trivial"],
                    "description": "Bug severity level",
                },
                "environment": {
                    "type": "string",
                    "enum": ["production", "staging", "development", "local"],
                    "description": "Environment where the bug was observed",
                },
                "browser": {
                    "type": "string",
                    "description": "Browser or client (e.g. Chrome 125, Safari 18, curl)",
                },
                "steps_to_reproduce": {
                    "type": "string",
                    "description": "Steps to reproduce the bug",
                },
            },
        },
    },
    {
        "slug": "content-marketing",
        "name": "Content & Marketing",
        "description": "Manage content production with type, audience, publish dates, and status tracking.",
        "icon": "megaphone",
        "is_builtin": True,
        "schema_definition": {
            "type": "object",
            "properties": {
                "content_type": {
                    "type": "string",
                    "enum": ["blog", "social", "email", "landing_page", "video", "whitepaper", "case_study"],
                    "description": "Type of content being produced",
                },
                "target_audience": {
                    "type": "string",
                    "description": "Intended audience for this content",
                },
                "publish_date": {
                    "type": "string",
                    "format": "date",
                    "description": "Planned or actual publish date (YYYY-MM-DD)",
                },
                "content_status": {
                    "type": "string",
                    "enum": ["idea", "draft", "in_review", "approved", "published"],
                    "description": "Content production status",
                },
            },
        },
    },
    {
        "slug": "sprint-planning",
        "name": "Sprint Planning",
        "description": "Track story points, sprints, and epics for agile workflows.",
        "icon": "sprint",
        "is_builtin": True,
        "schema_definition": {
            "type": "object",
            "properties": {
                "story_points": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Estimated effort in story points",
                },
                "sprint": {
                    "type": "string",
                    "description": "Sprint name or number (e.g. Sprint 14, 2026-W15)",
                },
                "epic": {
                    "type": "string",
                    "description": "Epic or initiative this issue belongs to",
                },
            },
        },
    },
    {
        "slug": "research-documentation",
        "name": "Research & Documentation",
        "description": "Organize research with source links, reviewers, confidence levels, and tags.",
        "icon": "book",
        "is_builtin": True,
        "schema_definition": {
            "type": "object",
            "properties": {
                "source_url": {
                    "type": "string",
                    "format": "uri",
                    "description": "URL of the source material",
                },
                "reviewed_by": {
                    "type": "string",
                    "description": "Person who reviewed or verified this",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "Confidence level in the information",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Free-form tags for categorization",
                },
            },
        },
    },
]


async def seed_metadata_presets(session: AsyncSession) -> None:
    """Upsert built-in metadata presets."""
    for data in _DEFAULT_METADATA_PRESETS:
        result = await session.execute(select(MetadataPreset).where(MetadataPreset.slug == data["slug"]))
        preset = result.scalar_one_or_none()
        if preset is None:
            preset = MetadataPreset(**data)
            session.add(preset)
            logger.info("Created metadata preset: %s", data["slug"])
        else:
            # Update builtin presets to latest definition
            if preset.is_builtin:
                for key, value in data.items():
                    setattr(preset, key, value)
                logger.info("Updated metadata preset: %s", data["slug"])

    await session.flush()
    print(f"Seeded {len(_DEFAULT_METADATA_PRESETS)} metadata presets: {[p['slug'] for p in _DEFAULT_METADATA_PRESETS]}")


async def _run() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        # Order matters: statuses must exist before trackers reference them
        await seed_statuses(session)
        await seed_trackers(session)
        await seed_priorities(session)
        await seed_roles(session)
        await seed_time_entry_activities(session)
        await seed_workflow_transitions(session)
        await seed_embedding_model(session)
        await seed_metadata_presets(session)
        await seed_settings(session)
        await session.commit()

    # Clear the process-local lookup cache so any running worker picks up
    # freshly seeded trackers/statuses/priorities/activities on the next request.
    from specivo.core.lookup_cache import invalidate_lookups

    invalidate_lookups()

    await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
