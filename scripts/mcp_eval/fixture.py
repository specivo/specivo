"""Fixture data for the MCP evaluation: lookups, an administrator with an API key, two projects.

Everything is committed to the database ``DATABASE_URL`` points at, so run the
harness against a throwaway database (the test database, reset afterwards).
Every run creates fresh projects with a random key, so runs never collide.

The lookups come from the ``specivo.cli.seed`` upserts; the projects are made
by ``ProjectService``; issues, wiki pages, a version, a sprint, a tag, a
metadata schema, a relation and a recurring pattern are created through the
MCP tools themselves (``populate``), as the administrator.
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from specivo.cli import seed
from specivo.core.database import get_session_factory
from specivo.core.lookup_cache import invalidate_lookups
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.recurring_pattern import RecurringPattern
from specivo.models.relation import IssueRelation
from specivo.models.sprint import Sprint
from specivo.models.time_entry import TimeEntryActivity
from specivo.models.user import User
from specivo.models.version import Version
from specivo.schemas.project import ProjectCreate
from specivo.services.api_key_service import ApiKeyService
from specivo.services.project_service import ProjectService


@dataclass
class Fixture:
    """The keys and IDs scenarios refer to. ``P`` is the main project, ``Q`` a second one."""

    P: str
    Q: str
    raw_key: str
    login: str
    ids: dict[str, int] = field(default_factory=dict)

    def text(self, template: str) -> str:
        """*template* with ``{P}``, ``{Q}`` and ``{login}`` filled in."""
        return template.format(P=self.P, Q=self.Q, login=self.login)


def _suffix() -> str:
    return "".join(secrets.choice(string.ascii_uppercase) for _ in range(4))


async def create_fixture() -> Fixture:
    """Seed the lookups and create an administrator, an API key and two empty projects."""
    suffix = _suffix()
    factory = get_session_factory()
    async with factory() as session:
        await seed.seed_statuses(session)
        await seed.seed_trackers(session)
        await seed.seed_priorities(session)
        fresh = await seed.seed_roles(session)
        await seed.seed_non_member_role(session)
        await seed.seed_time_entry_activities(session)
        await seed.seed_workflow_transitions(session, fresh=fresh)
        await seed.seed_metadata_presets(session)
        await session.commit()
    invalidate_lookups()

    login = f"mcp-eval-{suffix.lower()}"
    async with factory() as session:
        user = User(
            login=login,
            email=f"{login}@example.invalid",
            display_name="MCP Eval Agent",
            status="active",
            is_admin=True,
            is_service_account=False,
            must_change_password=False,
        )
        session.add(user)
        await session.flush()
        _, raw_key = await ApiKeyService().create_key(session, user_id=user.id, name="mcp-eval")
        projects = ProjectService()
        for key, name in ((f"EV{suffix}", "Eval Main"), (f"EQ{suffix}", "Eval Other")):
            await projects.create(
                session, ProjectCreate(name=f"{name} {suffix}", identifier=key.lower(), key=key, is_public=False)
            )
        await session.commit()
        user_id = user.id
    return Fixture(P=f"EV{suffix}", Q=f"EQ{suffix}", raw_key=raw_key, login=login, ids={"user": user_id})


#: Tool calls that fill the main project, in order. Issue numbers follow the order.
SETUP_CALLS: list[tuple[str, dict[str, Any]]] = [
    (
        "specivo_create_metadata_schema",
        {
            "project_key": "{P}",
            "name": "Development",
            "schema": {
                "type": "object",
                "properties": {
                    "component": {"type": "string"},
                    "commits": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    ),
    ("specivo_create_tag", {"project_key": "{P}", "name": "backend"}),
    ("specivo_create_version", {"project_key": "{P}", "name": "1.0", "due_date": "2027-01-31"}),
    ("specivo_create_sprint", {"project_key": "{P}", "name": "Sprint 1", "start_date": "2027-01-04"}),
    (
        "specivo_create_issue",  # 1
        {
            "project_key": "{P}",
            "tracker_id": "@tracker_bug",
            "subject": "Login page crashes on Safari",
            "description": "## Steps\n\nOpen the login page in Safari 17 and submit the form.\n",
        },
    ),
    (
        "specivo_create_issue",  # 2
        {"project_key": "{P}", "tracker_id": "@tracker_feature", "subject": "Add CSV export of the issue list"},
    ),
    (
        "specivo_create_issue",  # 3
        {"project_key": "{P}", "tracker_id": "@tracker_feature", "subject": "Upgrade the database driver"},
    ),
    (
        "specivo_create_issue",  # 4
        {"project_key": "{P}", "tracker_id": "@tracker_task", "subject": "Write the release notes"},
    ),
    (
        "specivo_create_issue",  # 5
        {"project_key": "{P}", "tracker_id": "@tracker_bug", "subject": "Flaky test in the CI pipeline"},
    ),
    (
        "specivo_create_issue",  # 6
        {"project_key": "{P}", "tracker_id": "@tracker_task", "subject": "Archive the old build scripts"},
    ),
    (
        "specivo_create_issue",  # 7: in version 1.0
        {
            "project_key": "{P}",
            "tracker_id": "@tracker_feature",
            "subject": "Single sign-on with the company directory",
            "fixed_version_id": "@version_1",
        },
    ),
    ("specivo_add_relation", {"issue_ref": "{P}-1", "issue_to_key": "{P}-5", "relation_type": "relates"}),
    ("specivo_add_comment", {"issue_ref": "{P}-2", "notes": "The export should include custom metadata."}),
    (
        "specivo_create_wiki",
        {
            "project_key": "{P}",
            "title": "Architecture Overview",
            "text": "## Components\n\nAPI, worker and database.\n\n## Deployment\n\nDeployed by hand on one host.\n\n"
            "Upgrades are tracked in {P}-3.\n",
        },
    ),
    (
        "specivo_create_wiki",
        {"project_key": "{P}", "title": "Release Process", "text": "We release weekly from the release branch.\n"},
    ),
    ("specivo_create_wiki", {"project_key": "{P}", "title": "Old Notes", "text": "Notes from the first sprint.\n"}),
    ("specivo_create_wiki", {"project_key": "{P}", "title": "Scratch Pad", "text": "Temporary notes.\n"}),
    (
        "specivo_create_recurring_pattern",
        {
            "project_key": "{P}",
            "name": "Weekly status report",
            "template_subject": "Status report {{day}} {{month}}",
            "template_tracker_id": "@tracker_task",
            "freq": "weekly",
            "byday": "FR",
            "dtstart": "2026-01-02T09:00:00",
            "timezone": "Asia/Bangkok",
        },
    ),
]


def fill(value: Any, fixture: Fixture) -> Any:
    """*value* with ``{P}``/``{Q}``/``{login}`` filled in and ``"@name"`` replaced by ``fixture.ids[name]``."""
    if isinstance(value, str):
        if value.startswith("@") and value[1:] in fixture.ids:
            return fixture.ids[value[1:]]
        if "{{" in value:  # date macros of recurring patterns stay as they are
            return value.replace("{P}", fixture.P).replace("{Q}", fixture.Q)
        return fixture.text(value)
    if isinstance(value, list):
        return [fill(item, fixture) for item in value]
    if isinstance(value, dict):
        return {key: fill(item, fixture) for key, item in value.items()}
    return value


async def load_lookup_ids(fixture: Fixture) -> None:
    """Record the IDs of the lookups the setup and the scenarios refer to."""
    async with get_session_factory()() as session:
        for model, prefix, names in (
            (Tracker, "tracker", ("Bug", "Feature", "Task")),
            (IssueStatus, "status", ("New", "In Progress", "Resolved", "Closed")),
            (IssuePriority, "priority", ("Normal", "High")),
            (TimeEntryActivity, "activity", ("Development", "Testing")),
        ):
            for name in names:
                row = await session.scalar(select(model.id).where(model.name == name))
                if row is None:
                    raise RuntimeError(f"Lookup {name!r} is missing after seeding")
                fixture.ids[f"{prefix}_{name.lower().replace(' ', '_')}"] = int(row)


async def load_created_ids(fixture: Fixture) -> None:
    """Record the IDs of what ``SETUP_CALLS`` created."""
    async with get_session_factory()() as session:
        project_id = await session.scalar(select(Project.id).where(Project.key == fixture.P))
        version = await session.scalar(
            select(Version.id).where(Version.project_id == project_id, Version.name == "1.0")
        )
        sprint = await session.scalar(
            select(Sprint.id).where(Sprint.project_id == project_id, Sprint.name == "Sprint 1")
        )
        relation = await session.scalar(select(IssueRelation.id).order_by(IssueRelation.id.desc()).limit(1))
        pattern = await session.scalar(
            select(RecurringPattern.id).where(
                RecurringPattern.project_id == project_id, RecurringPattern.name == "Weekly status report"
            )
        )
    fixture.ids.update(
        {"version_1": int(version or 0), "sprint_1": int(sprint or 0), "relation_1_5": int(relation or 0)}
    )
    fixture.ids["pattern_weekly"] = int(pattern or 0)
