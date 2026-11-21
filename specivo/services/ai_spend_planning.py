"""The release (version) and sprint that AI usage and logged time count towards.

An entry belongs to the release and the sprint of its issue **now** (no
snapshot is taken when it is logged), so re-planning an issue moves its spend
with it. An issue without a version of its own takes the version of its
nearest ancestor that has one; the same goes for sprints. The issue's own
value always wins over an ancestor's.

:func:`effective_planning` is that rule as one correlated scalar subquery: the
issue joined once to its ancestors through the nested-set columns
(``root_id``, ``lft``, ``rgt``; indexed by ``ix_issues_root_id_lft``), the
nearest one first. No recursive query. It is a self-contained expression over
an outer issue id column, so a report can group by it, filter on it and count
people per value with the WHERE conditions it already has, and the sprint
page can sum time entries and estimates with it.

Visibility is part of the expression, so a row never names what its reader
may not see, and a "none" filter matches exactly the entries of the "none"
row:

- an issue the reader cannot see has no release and no sprint (entries on it
  count only when they are the reader's own, and then under "No release");
- a version or sprint inherited from an ancestor the reader cannot see is not
  inherited;
- a version (shared ones included) or sprint of a project the reader cannot
  open counts as none.

The reader's visibility is resolved once per report (:func:`planning_visibility`):
a couple of queries, whatever the data.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, any_, case, func, inspect, or_, select, text, union
from sqlalchemy.orm import aliased
from sqlalchemy.sql.util import ClauseAdapter

from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.sprint import Sprint
from specivo.models.version import Version

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select
    from sqlalchemy.sql.elements import ColumnElement

    from specivo.models.user import User

#: The planning dimensions of a report: an entry's release and sprint.
PLANNING_DIMENSIONS: tuple[str, ...] = ("version", "sprint")

#: Per dimension: the issue column holding it and the model it points at.
_ATTRIBUTES: dict[str, tuple[str, type[Version] | type[Sprint]]] = {
    "version": ("fixed_version_id", Version),
    "sprint": ("sprint_id", Sprint),
}


@dataclass(frozen=True, slots=True)
class PlanningFilter:
    """Keep the entries of one release or sprint, or of none (both optional).

    An id the reader may not see matches nothing: the lookup never yields it.
    """

    version_id: int | None = None
    without_version: bool = False
    sprint_id: int | None = None
    without_sprint: bool = False

    @property
    def active(self) -> bool:
        return bool(
            self.version_id is not None or self.without_version or self.sprint_id is not None or self.without_sprint
        )

    def wanted(self) -> list[tuple[str, int | None, bool]]:
        """``(dimension, id, none)`` per dimension this filter narrows."""
        out: list[tuple[str, int | None, bool]] = []
        if self.version_id is not None or self.without_version:
            out.append(("version", self.version_id, self.without_version))
        if self.sprint_id is not None or self.without_sprint:
            out.append(("sprint", self.sprint_id, self.without_sprint))
        return out


@dataclass(frozen=True)
class PlanningVisibility:
    """What a reader may see, as predicates: issues (over ``issues``) and projects (over ``projects``).

    None means no limit (administrators).
    """

    issues: ColumnElement[bool] | None
    projects: ColumnElement[bool] | None


#: ``session.info`` key: the transaction in which JIT was last turned off (:func:`_without_jit`).
_JIT_OFF_KEY = "specivo.planning_jit_off"


async def _without_jit(session: AsyncSession) -> None:
    """Turn PostgreSQL's JIT off for the rest of the current transaction (once per transaction).

    The planner cannot tell that the lookup is memoized per issue and
    estimates it per entry, so a statement over a few thousand entries passes
    ``jit_above_cost`` and spends far longer compiling (0.4 to 0.9 s on a
    year of a project) than running (tens of ms). ``SET LOCAL`` ends with
    the transaction, so it never reaches another request.
    """
    transaction = session.get_transaction()
    if transaction is None or session.info.get(_JIT_OFF_KEY) is transaction:
        return
    await session.execute(text("SET LOCAL jit = off"))
    session.info[_JIT_OFF_KEY] = transaction


async def planning_visibility(session: AsyncSession, viewer: User) -> PlanningVisibility:
    """*viewer*'s issue and project visibility, for :func:`planning_lookup`.

    Every planning lookup is built with it, so it also turns JIT off for the
    transaction (:func:`_without_jit`).
    """
    from specivo.services.issue_service import IssueService
    from specivo.services.project_service import ProjectService

    await _without_jit(session)
    return PlanningVisibility(
        issues=await IssueService().visible_issues_clause(session, viewer),
        projects=await ProjectService().accessible_projects_clause(session, viewer),
    )


def _on(clause: ColumnElement[bool], alias: Any) -> ColumnElement[bool]:
    """*clause*, a predicate over ``issues``, rewritten for the issue *alias*."""
    adapted: ColumnElement[bool] = ClauseAdapter(inspect(alias).selectable).traverse(clause)
    return adapted


def planning_lookup(dim: str, issue_id: Any, visibility: PlanningVisibility) -> Select[Any]:
    """One row (or none) holding the id of the version (*dim* ``version``) or sprint of issue *issue_id*.

    *issue_id* is a column of the outer query (``AiUsageEntry.issue_id``,
    ``TimeEntry.issue_id``, ``Issue.id``); the column is labelled *dim*. The
    issue's own value, else the value of its nearest ancestor that has one:
    the issue joined once to its ancestors on ``root_id`` and the
    ``lft``/``rgt`` bounds (``ix_issues_root_id_lft``), the largest ``lft``
    first, one row. NULL (or no row) without an issue, for an issue the
    reader cannot see, for a value inherited from an ancestor they cannot see,
    and for a value of a project they cannot open.

    Used as a ``LEFT JOIN LATERAL`` to group by it (:func:`planning_joins`;
    PostgreSQL memoizes it per issue) and as a scalar subquery to filter on it
    (:func:`planning_conditions`), so a group and its filter always agree.
    """
    column, target = _ATTRIBUTES[dim]
    issue = aliased(Issue, name=f"spend_{dim}_issue")
    ancestor = aliased(Issue, name=f"spend_{dim}_ancestor")
    own: ColumnElement[Any] = getattr(issue, column)
    inherited: ColumnElement[Any] = getattr(ancestor, column)
    if visibility.issues is not None:
        # The nearest ancestor decides; one the reader cannot see passes nothing on.
        inherited = case((_on(visibility.issues, ancestor), inherited))
    picked: Any = func.coalesce(own, inherited)
    correlated: list[Any] = [issue, ancestor]
    value = None
    if visibility.projects is not None:
        # Only a reader limited to some projects needs the value's project.
        value = aliased(target, name=f"spend_{dim}_value")
        readable = select(Project.id).where(visibility.projects)
        picked = case((or_(value.project_id == issue.project_id, value.project_id.in_(readable)), value.id))
        correlated.append(value)
    stmt = (
        select(picked.label(dim))
        .select_from(issue)
        .outerjoin(
            ancestor,
            and_(
                own.is_(None),
                ancestor.root_id == issue.root_id,
                ancestor.lft < issue.lft,
                ancestor.rgt > issue.rgt,
                getattr(ancestor, column).is_not(None),
            ),
        )
        .where(issue.id == issue_id)
        .order_by(ancestor.lft.desc().nulls_last())
        .limit(1)
    )
    if value is not None:
        stmt = stmt.outerjoin(value, value.id == func.coalesce(own, inherited))
    if visibility.issues is not None:
        stmt = stmt.where(_on(visibility.issues, issue))
    return stmt.correlate_except(*correlated)


def effective_planning(dim: str, issue_id: Any, visibility: PlanningVisibility) -> ColumnElement[Any]:
    """:func:`planning_lookup` as a scalar: the id, or NULL."""
    return planning_lookup(dim, issue_id, visibility).scalar_subquery()


def planning_joins(
    dims: Iterable[str], issue_id: Any, visibility: PlanningVisibility
) -> tuple[dict[str, Any], list[Any]]:
    """For grouping by the planning dimensions in *dims*: their columns, and the laterals to outer-join.

    Join each lateral with ``outerjoin(lateral, true())`` after the table of
    *issue_id*; an entry without a row gets NULL.
    """
    columns: dict[str, Any] = {}
    laterals: list[Any] = []
    for dim in dims:
        if dim in _ATTRIBUTES:
            lateral = planning_lookup(dim, issue_id, visibility).lateral(f"spend_{dim}")
            columns[dim] = lateral.c[dim]
            laterals.append(lateral)
    return columns, laterals


def candidates(dim: str, value_id: int, issue_id: Any) -> ColumnElement[bool]:
    """A cheap superset of the issues whose release or sprint (*dim*) is *value_id*: theirs, or their tree's.

    The value comes from the issue or an ancestor, which shares its
    ``root_id``. Index scans only (``ix_issues_fixed_version_id`` /
    ``ix_issues_sprint_id``, then ``ix_issues_root_id_lft``), computed once
    per statement, so the lookup runs only for the entries that may match.
    """
    column, _ = _ATTRIBUTES[dim]
    planned = aliased(Issue, name=f"planned_{dim}")
    member = aliased(Issue, name=f"member_{dim}")
    roots = select(planned.root_id).where(getattr(planned, column) == value_id, planned.root_id.is_not(None))
    # Two index scans; an OR of the two would scan every issue.
    tree = union(
        select(member.id).where(member.root_id.in_(roots)),
        select(planned.id).where(getattr(planned, column) == value_id),
    )
    # ``= ANY(ARRAY(...))``: computed once and checked as a cheap filter, before the lookup runs.
    narrowed: ColumnElement[bool] = issue_id == any_(func.array(tree.scalar_subquery()))
    return narrowed


def planning_conditions(
    planning: PlanningFilter, issue_id: Any, visibility: PlanningVisibility
) -> list[ColumnElement[bool]]:
    """The WHERE conditions of *planning* over the outer column *issue_id* (an entry's issue, or ``Issue.id``)."""
    out: list[ColumnElement[bool]] = []
    for dim, wanted, none in planning.wanted():
        lookup = effective_planning(dim, issue_id, visibility)
        if wanted is not None:
            out.extend([candidates(dim, wanted, issue_id), lookup == wanted])
        if none:
            out.append(lookup.is_(None))
    return out


async def sprint_issue_conditions(session: AsyncSession, viewer: User, sprint_id: int) -> list[ColumnElement[bool]]:
    """Conditions over ``Issue`` keeping the issues counted in sprint *sprint_id* for *viewer* (own or inherited)."""
    visibility = await planning_visibility(session, viewer)
    return planning_conditions(PlanningFilter(sprint_id=sprint_id), Issue.id, visibility)


def planning_label(name: str, owner_id: int, owner_key: str, project: Project | None) -> str:
    """How a release or sprint is named in a report of *project*: ``name``, or ``KEY name`` from elsewhere.

    Reports of the instance or of a set of projects always prefix the key.
    """
    return name if project is not None and owner_id == project.id else f"{owner_key} {name}"


async def planning_labels(session: AsyncSession, dim: str, ids: set[Any], project: Project | None) -> dict[int, str]:
    """The labels (:func:`planning_label`) of the versions or sprints *ids* (one query).

    Only for ids :func:`effective_planning` returned: it never yields one the
    reader may not see.
    """
    if not ids:
        return {}
    _, target = _ATTRIBUTES[dim]
    stmt = (
        select(target.id, target.name, target.project_id, Project.key)
        .join(Project, Project.id == target.project_id)
        .where(target.id.in_(sorted(ids)))
    )
    return {
        row_id: planning_label(name, owner_id, key, project)
        for row_id, name, owner_id, key in (await session.execute(stmt)).all()
    }


@dataclass(frozen=True, slots=True)
class ResolvedPlanning:
    """A release or sprint filter value resolved: the id to filter by (-1: nothing) and its label."""

    id: int
    label: str


async def resolve_planning(
    session: AsyncSession, viewer: User, dim: str, value: str, project: Project | None
) -> ResolvedPlanning:
    """The version or sprint a filter value names, within what *viewer* may see.

    *value* is an id, or (with *project*) a name: a version visible from the
    project (shared ones included, the project's own first) or a sprint of
    the project, matched without regard to case. An id is any version or
    sprint of a project *viewer* may open, since a sub-issue may inherit one
    from another project. Anything else resolves to id -1, so the filter
    matches nothing rather than being ignored; the label is then the value
    as given.
    """
    from specivo.services.project_service import ProjectService

    _, target = _ATTRIBUTES[dim]
    text = value.strip()
    found: Any = None
    if text.isdecimal():
        stmt = select(target).where(target.id == int(text))
        accessible = await ProjectService().accessible_projects_clause(session, viewer)
        if accessible is not None:
            stmt = stmt.where(target.project_id.in_(select(Project.id).where(accessible)))
        found = await session.scalar(stmt)
    elif project is not None and text:
        found = await _by_name(session, viewer, dim, text, project)
    if found is None:
        return ResolvedPlanning(id=-1, label=text)
    owner = project if project is not None and found.project_id == project.id else None
    key = (
        owner.key
        if owner is not None
        else await session.scalar(select(Project.key).where(Project.id == found.project_id))
    )
    return ResolvedPlanning(id=found.id, label=planning_label(found.name, found.project_id, str(key), project))


async def _by_name(session: AsyncSession, viewer: User, dim: str, name: str, project: Project) -> Any:
    if dim == "sprint":
        stmt = select(Sprint).where(Sprint.project_id == project.id, func.lower(Sprint.name) == name.casefold())
        return await session.scalar(stmt.order_by(Sprint.id).limit(1))
    from specivo.services.version_service import VersionService

    matches = [
        v
        for v in await VersionService().visible_versions(session, project, viewer)
        if v.name.casefold() == name.casefold()
    ]
    # A name shared by this project's own version and a shared one: the project's own wins.
    matches.sort(key=lambda v: (v.project_id != project.id, v.id))
    return matches[0] if matches else None


__all__ = [
    "PLANNING_DIMENSIONS",
    "PlanningFilter",
    "PlanningVisibility",
    "ResolvedPlanning",
    "candidates",
    "effective_planning",
    "planning_conditions",
    "planning_joins",
    "planning_lookup",
    "planning_label",
    "planning_labels",
    "planning_visibility",
    "resolve_planning",
    "sprint_issue_conditions",
]
