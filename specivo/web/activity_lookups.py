"""Names for the ids an issue's activity feed shows.

A field change in an issue's history keeps the ids of the records it names
(``status_id``, ``fixed_version_id``, …), and the feed shows the records'
names. The issue page and the activity partial that pages through the feed
build the same maps here, so a change reads the same on either.

A value that is not an id (imported history keeps the source's text for a
record that was not imported) has no entry, and the feed shows it as stored.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.journal import Journal
from specivo.models.lookups import IssueCategory
from specivo.models.project import Project
from specivo.models.sprint import Sprint
from specivo.models.user import User
from specivo.models.version import Version
from specivo.services.permission_service import visible_users_clause
from specivo.services.project_service import ProjectService

# Attributes whose names are looked up from the ids the history holds.
_LOADED_ATTRIBUTES = ("sprint_id", "fixed_version_id", "category_id", "project_id", "assigned_to_id")

_project_svc = ProjectService()


def referenced_ids(journals: Iterable[Journal]) -> dict[str, set[int]]:
    """Return the ids the journals' field changes name, by attribute, ignoring values that are not ids."""
    referenced: dict[str, set[int]] = {key: set() for key in _LOADED_ATTRIBUTES}
    for journal in journals:
        for detail in journal.details:
            ids = referenced.get(detail.prop_key)
            if ids is None:
                continue
            for value in (detail.old_value, detail.new_value):
                if value is not None and str(value).isdigit():
                    ids.add(int(value))
    return referenced


async def activity_lookup_maps(
    db: AsyncSession,
    user: User,
    journals: Iterable[Journal],
    *,
    statuses: Sequence[Any],
    trackers: Sequence[Any],
    priorities: Sequence[Any],
    members: Sequence[dict[str, Any]],
    sprint_id: int | None = None,
) -> dict[str, dict[str, str]]:
    """Return ``{attribute: {id as text: name}}`` for the activity feed of *journals*.

    Statuses, trackers and priorities come from the lists given; versions,
    sprints and categories are loaded for the ids the history names. A project
    (imported history records moves between projects) is named only when
    *user* may open it, and a former assignee who is no longer a member only
    when *user* may see them. Signed-in readers see people by login, an
    anonymous visitor by display name.
    """
    referenced = referenced_ids(journals)
    if sprint_id is not None:
        referenced["sprint_id"].add(sprint_id)

    return {
        "status_id": {str(s.id): s.name for s in statuses},
        "tracker_id": {str(t.id): t.name for t in trackers},
        "priority_id": {str(p.id): p.name for p in priorities},
        "assigned_to_id": await _people(db, user, referenced["assigned_to_id"], members),
        "sprint_id": await _names(db, Sprint, referenced["sprint_id"]),
        "fixed_version_id": await _names(db, Version, referenced["fixed_version_id"]),
        "category_id": await _names(db, IssueCategory, referenced["category_id"]),
        "project_id": await _projects(db, user, referenced["project_id"]),
    }


async def _names(db: AsyncSession, model: Any, ids: set[int]) -> dict[str, str]:
    if not ids:
        return {}
    rows = (await db.execute(select(model.id, model.name).where(model.id.in_(ids)))).all()
    return {str(row.id): row.name for row in rows}


async def _projects(db: AsyncSession, user: User, ids: set[int]) -> dict[str, str]:
    if not ids:
        return {}
    stmt = select(Project.id, Project.name).where(Project.id.in_(ids))
    accessible = await _project_svc.accessible_projects_clause(db, user)
    if accessible is not None:
        stmt = stmt.where(accessible)
    return {str(row.id): row.name for row in (await db.execute(stmt)).all()}


async def _people(db: AsyncSession, user: User, ids: set[int], members: Sequence[dict[str, Any]]) -> dict[str, str]:
    if user.is_anonymous:
        # The member list names people by login. An anonymous visitor gets
        # display names, and only for the people the feed actually mentions.
        if not ids:
            return {}
        rows = (await db.execute(select(User.id, User.display_name).where(User.id.in_(ids)))).all()
        return {str(row.id): row.display_name for row in rows}

    people = {str(m.get("user_id", m.get("id", ""))): m.get("login", m.get("display_name", "")) for m in members}
    # History can name people who are no longer members (imported history
    # often does); they are named when the reader may see them.
    former = {uid for uid in ids if str(uid) not in people}
    if former:
        stmt = select(User.id, User.login).where(User.id.in_(former))
        visible = await visible_users_clause(db, user)
        if visible is not None:
            stmt = stmt.where(visible)
        people.update({str(row.id): row.login for row in (await db.execute(stmt)).all()})
    return people
