"""Names for the ids an issue's activity feed shows.

A field change in an issue's history keeps the ids of the records it names
(``status_id``, ``fixed_version_id``, …), and the feed shows the records'
names. The issue page and the activity partial that pages through the feed
build the same maps here, so a change reads the same on either.

A value that is not an id (imported history keeps the source's text for a
record that was not imported) has no entry, and the feed shows it as stored.

Imported custom field changes whose values name a person or a version
(``cf_user`` / ``cf_version`` details) are named by the same maps, and by the
same rules, as an assignee or a target version.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue, IssueRefAlias
from specivo.models.journal import CF_USER, CF_VERSION, Journal, JournalDetail
from specivo.models.lookups import IssueCategory
from specivo.models.project import Project, ProjectKeyAlias
from specivo.models.sprint import Sprint
from specivo.models.user import User
from specivo.models.version import Version
from specivo.services.permission_service import visible_users_clause
from specivo.services.project_service import ProjectService

# Attributes whose names are looked up from the ids the history holds.
_LOADED_ATTRIBUTES = ("sprint_id", "fixed_version_id", "category_id", "project_id", "assigned_to_id")

_project_svc = ProjectService()

# A value that reads as an issue's display key, ``KEY-N``.
_ISSUE_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)-(\d+)$")

# The lookup map holding the parent keys the feed leaves out, as ``{key: ""}``.
HIDDEN_PARENTS = "parent_id_hidden"

# Attributes whose values are ids the feed names.
_NAMED_ATTRIBUTES = frozenset({"status_id", "tracker_id", "priority_id", *_LOADED_ATTRIBUTES})
# Custom field changes whose values are ids, and the attribute whose map names them.
_NAMED_CUSTOM_FIELDS = {CF_USER: "assigned_to_id", CF_VERSION: "fixed_version_id"}


def lookup_key(prop: str, prop_key: str) -> str | None:
    """Return the map naming a field change's values, or ``None`` when they are not ids."""
    if prop == "attr":
        return prop_key if prop_key in _NAMED_ATTRIBUTES else None
    return _NAMED_CUSTOM_FIELDS.get(prop)


def is_parent_change(detail: JournalDetail) -> bool:
    return detail.property == "attr" and detail.prop_key == "parent_id"


class ActivityLookups(dict[str, dict[str, str]]):
    """``{map: {id as text: name}}``, and how a field change reads through it."""

    def name(self, detail: JournalDetail, value: str | None) -> str | None:
        """Return the name for one of *detail*'s values, or ``None`` to show the value as stored."""
        key = lookup_key(detail.property, detail.prop_key)
        if key is None or value is None:
            return None
        return self.get(key, {}).get(str(value))

    def hides(self, detail: JournalDetail, *, anonymous: bool) -> bool:
        """Whether the feed leaves *detail* out for this reader.

        A relation change names another issue by key, and a visitor without an
        account is shown none of them, nor any parent change. For anyone else a
        parent change is left out when its key does not resolve to an issue they
        may see (see :func:`_hidden_parents`); without that list, every one is.
        """
        if anonymous and (detail.property == "relation" or is_parent_change(detail)):
            return True
        if not is_parent_change(detail):
            return False
        hidden = self.get(HIDDEN_PARENTS)
        return hidden is None or detail.old_value in hidden or detail.new_value in hidden


def referenced_ids(journals: Iterable[Journal]) -> dict[str, set[int]]:
    """Return the ids the journals' field changes name, by lookup map, ignoring values that are not ids."""
    referenced: dict[str, set[int]] = {key: set() for key in _LOADED_ATTRIBUTES}
    for journal in journals:
        for detail in journal.details:
            key = lookup_key(detail.property, detail.prop_key)
            ids = referenced.get(key) if key is not None else None
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
) -> ActivityLookups:
    """Return ``{map: {id as text: name}}`` for the activity feed of *journals*.

    Statuses, trackers and priorities come from the lists given; versions,
    sprints and categories are loaded for the ids the history names, and named
    only when *user* may open their project; so is a project (imported history
    records moves between projects). A parent change naming an issue *user*
    may not see is listed under :data:`HIDDEN_PARENTS` for the feed to leave
    out. A former assignee who is no longer a member only
    when *user* may see them. Signed-in readers see people by login, an
    anonymous visitor by display name. People and versions an imported custom
    field change names are named by the same rules.
    """
    referenced = referenced_ids(journals)
    if sprint_id is not None:
        referenced["sprint_id"].add(sprint_id)

    return ActivityLookups(
        {
            "status_id": {str(s.id): s.name for s in statuses},
            "tracker_id": {str(t.id): t.name for t in trackers},
            "priority_id": {str(p.id): p.name for p in priorities},
            "assigned_to_id": await _people(db, user, referenced["assigned_to_id"], members),
            "sprint_id": await _names(db, user, Sprint, referenced["sprint_id"]),
            "fixed_version_id": await _names(db, user, Version, referenced["fixed_version_id"]),
            "category_id": await _names(db, user, IssueCategory, referenced["category_id"]),
            "project_id": await _projects(db, user, referenced["project_id"]),
            HIDDEN_PARENTS: await _hidden_parents(db, user, journals),
        }
    )


async def _names(db: AsyncSession, user: User, model: Any, ids: set[int]) -> dict[str, str]:
    """Name the records of *model* (each belongs to a project) whose project *user* may open.

    The rule version sharing follows too: sharing decides which projects a
    version reaches, never that a reader sees one whose own project they
    cannot open. An id left out shows as stored.
    """
    if not ids:
        return {}
    stmt = select(model.id, model.name).join(Project, Project.id == model.project_id).where(model.id.in_(ids))
    accessible = await _project_svc.accessible_projects_clause(db, user)
    if accessible is not None:
        stmt = stmt.where(accessible)
    return {str(row.id): row.name for row in (await db.execute(stmt)).all()}


async def _hidden_parents(db: AsyncSession, user: User, journals: Iterable[Journal]) -> dict[str, str]:
    """Return the parent changes' values that look like an issue key *user* may not be shown.

    History keeps a parent by display key, and the page links any key it
    shows, resolving it as the app resolves references: the current key, then
    the aliases a move (``IssueRefAlias``) or a project key rename
    (``ProjectKeyAlias``) left. A key is shown only when it resolves that way
    to an issue *user* may see; any other key-like value is listed, so the
    feed leaves the change out entirely, as the relations list leaves out a
    relation to an issue the reader cannot see. Fails closed: a key naming no
    issue at all is hidden too. A value that is no key (``#123``) stays.
    Admins see every issue, so nothing is hidden from them.
    """
    keys: dict[tuple[str, int], set[str]] = {}
    for journal in journals:
        for detail in journal.details:
            if not is_parent_change(detail):
                continue
            for value in (detail.old_value, detail.new_value):
                match = _ISSUE_KEY.match(value or "")
                if match:
                    keys.setdefault((match[1].upper(), int(match[2])), set()).add(str(value))
    if not keys or user.is_admin:
        return {}

    resolved = await _resolve_issue_keys(db, set(keys))
    visible: set[int] = set()
    if resolved:
        from specivo.services.issue_service import IssueService

        stmt = select(Issue.id).where(Issue.id.in_(set(resolved.values())))
        clause = await IssueService().visible_issues_clause(db, user)
        if clause is not None:
            stmt = stmt.where(clause)
        visible = set((await db.execute(stmt)).scalars())
    return {value: "" for key, values in keys.items() if resolved.get(key) not in visible for value in values}


async def _resolve_issue_keys(db: AsyncSession, keys: set[tuple[str, int]]) -> dict[tuple[str, int], int]:
    """Return the issue id each ``(project key, number)`` names, as ``IssueService.get_by_display_key`` finds it."""
    resolved: dict[tuple[str, int], int] = {}
    rows = await db.execute(
        select(Issue.project_key, Issue.sequence_number, Issue.id).where(
            tuple_(Issue.project_key, Issue.sequence_number).in_(keys)
        )
    )
    resolved.update({(key, number): issue_id for key, number, issue_id in rows.all()})

    missing = keys - resolved.keys()
    if missing:
        rows = await db.execute(
            select(IssueRefAlias.old_project_key, IssueRefAlias.old_sequence_number, IssueRefAlias.issue_id).where(
                tuple_(IssueRefAlias.old_project_key, IssueRefAlias.old_sequence_number).in_(missing)
            )
        )
        resolved.update({(key, number): issue_id for key, number, issue_id in rows.all()})

    missing = keys - resolved.keys()
    if missing:
        renamed = dict(
            (
                await db.execute(
                    select(ProjectKeyAlias.old_key, ProjectKeyAlias.project_id).where(
                        ProjectKeyAlias.old_key.in_({key for key, _ in missing})
                    )
                )
            ).all()
        )
        wanted = {(renamed[key], number): (key, number) for key, number in missing if key in renamed}
        if wanted:
            rows = await db.execute(
                select(Issue.project_id, Issue.sequence_number, Issue.id).where(
                    tuple_(Issue.project_id, Issue.sequence_number).in_(set(wanted))
                )
            )
            for row in rows.all():
                resolved[wanted[(int(row.project_id), int(row.sequence_number))]] = int(row.id)
    return resolved


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
