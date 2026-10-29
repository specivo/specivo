"""Version service — CRUD, roadmap, and sharing-aware visibility."""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable, Iterable
from typing import Any, get_args

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError
from specivo.core.natural_sort import natural_sort_key
from specivo.models.issue import Issue
from specivo.models.lookups import IssueStatus
from specivo.models.project import Project
from specivo.models.user import User
from specivo.models.version import Version
from specivo.schemas.version import RoadmapEntry, VersionCreate, VersionOut, VersionSort, VersionUpdate
from specivo.services.issue_service import IssueService
from specivo.services.markdown_budget import check_markdown_length

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

#: Every accepted ``"<field>:<direction>"`` value, in the order a select offers
#: them. Fields: ``version`` (natural name), ``due`` (due date), ``status``.
VERSION_SORT_OPTIONS: tuple[str, ...] = get_args(VersionSort)
VERSION_SORT_DEFAULT = "due:asc"
#: Key in ``User.preferences`` that remembers a viewer's version sort.
VERSION_SORT_PREFERENCE = "version_sort"

_STATUS_RANK = {"open": 0, "locked": 1, "closed": 2}

#: Extracts ``(name, status, effective_date)`` from whatever is being sorted.
VersionAttrs = Callable[[Any], tuple[str, str, datetime.date | None]]


def _version_attrs(version: Version) -> tuple[str, str, datetime.date | None]:
    return version.name, version.status, version.effective_date


def normalize_version_sort(value: str | None) -> str | None:
    """Return *value* if it is a valid ``"<field>:<direction>"`` sort, else ``None``."""
    return value if value in VERSION_SORT_OPTIONS else None


def _sorted_within[T](items: list[T], field: str, descending: bool, attrs: VersionAttrs) -> list[T]:
    """Sort *items* by one field; ties fall back to due date, then natural name."""

    def name_key(item: T) -> tuple:
        return natural_sort_key(attrs(item)[0])

    if field == "version":
        return sorted(items, key=name_key, reverse=descending)

    # Due date: undated versions always go last, whichever direction is chosen;
    # ties keep the ascending natural-name order (Python's sort is stable, also
    # with reverse=True). A status sort uses ascending due date as its tiebreak.
    by_name = sorted(items, key=name_key)
    dated = [i for i in by_name if attrs(i)[2] is not None]
    undated = [i for i in by_name if attrs(i)[2] is None]
    dated.sort(key=lambda i: attrs(i)[2], reverse=descending and field == "due")  # type: ignore[arg-type,return-value]
    by_due = dated + undated
    if field == "due":
        return by_due
    return sorted(by_due, key=lambda i: _STATUS_RANK.get(attrs(i)[1], 0), reverse=descending)


def order_versions[T](
    items: Iterable[T],
    sort: str | None = None,
    *,
    attrs: VersionAttrs | None = None,
) -> list[T]:
    """Order versions for display.

    *sort* is a ``"<field>:<direction>"`` value from :data:`VERSION_SORT_OPTIONS`
    (invalid or missing values fall back to :data:`VERSION_SORT_DEFAULT`):

    - ``version`` — natural name order (``0.2.0 < 0.10.0 < 0.11.0``);
    - ``due`` — due date, undated versions last, then natural name;
    - ``status`` — open, locked, closed, then due date and natural name.

    For ``version`` and ``due`` the open and locked versions always come before
    the closed ones and the sort applies within each group, so the default is
    "open and locked by due date, then closed by due date". ``status`` orders
    the groups itself, so ``status:desc`` lists closed versions first.

    *attrs* maps an item to ``(name, status, effective_date)``; it defaults to
    reading a :class:`Version`. Names are compared in Python, so the order does
    not depend on the database collation.
    """
    field, direction = (normalize_version_sort(sort) or VERSION_SORT_DEFAULT).split(":")
    get: VersionAttrs = attrs or _version_attrs
    descending = direction == "desc"
    rows = list(items)
    if field == "status":
        return _sorted_within(rows, field, descending, get)
    active = [i for i in rows if get(i)[1] != "closed"]
    closed = [i for i in rows if get(i)[1] == "closed"]
    return _sorted_within(active, field, descending, get) + _sorted_within(closed, field, descending, get)


class VersionService:
    """Service layer for Version operations."""

    # -----------------------------------------------------------------------
    # CRUD
    # -----------------------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        project: Project,
        data: VersionCreate,
    ) -> Version:
        """Create a new version for *project*."""
        check_markdown_length(data.description, "description")
        version = Version(
            project_id=project.id,
            name=data.name,
            description=data.description,
            status=data.status,
            effective_date=data.effective_date,
            sharing=data.sharing,
            wiki_page_title=data.wiki_page_title,
        )
        session.add(version)
        await session.flush()
        return version

    async def get_by_id(
        self,
        session: AsyncSession,
        version_id: int,
    ) -> Version:
        """Return a Version by PK; raises NotFoundError if missing."""
        result = await session.execute(select(Version).where(Version.id == version_id))
        version = result.scalar_one_or_none()
        if version is None:
            raise NotFoundError(f"Version {version_id} not found")
        return version

    async def list_for_project(
        self,
        session: AsyncSession,
        project_id: int,
        sort: str | None = None,
    ) -> list[Version]:
        """List versions for a project in display order (see :func:`order_versions`).

        The default order is open and locked versions by due date (undated
        last), then natural name, followed by closed versions in the same order.
        A project has few versions, so the list is ordered in Python.
        """
        stmt = select(Version).where(Version.project_id == project_id)
        result = await session.execute(stmt)
        return order_versions(result.scalars().all(), sort)

    async def search_for_project(
        self,
        session: AsyncSession,
        project_id: int,
        query: str | None = None,
        limit: int = 20,
    ) -> list[Version]:
        """Search versions for a project (version picker / autocomplete).

        - Empty query returns up to 10 versions; non-empty query does a
          case-insensitive substring match on name, capped at ``limit`` rows.
        - Open and locked versions come first in the default order (due date,
          undated last, then natural name), so the versions you can still
          assign to lead the list. Closed versions follow, most recent first
          (latest due date, then highest version number).
        """
        stmt = select(Version).where(Version.project_id == project_id)
        q = (query or "").strip()
        if q:
            stmt = stmt.where(Version.name.ilike(f"%{q}%"))
        rows = list((await session.execute(stmt)).scalars().all())
        active = order_versions((v for v in rows if v.status != "closed"), VERSION_SORT_DEFAULT)
        closed = sorted(
            (v for v in rows if v.status == "closed"),
            key=lambda v: natural_sort_key(v.name),
            reverse=True,
        )
        # Undated closed versions map to date.min and so land last.
        closed.sort(key=lambda v: v.effective_date or datetime.date.min, reverse=True)
        return (active + closed)[: limit if q else 10]

    async def update(
        self,
        session: AsyncSession,
        version: Version,
        data: VersionUpdate,
    ) -> Version:
        """Apply a partial update to *version*."""
        check_markdown_length(data.description, "description")
        if data.name is not None:
            version.name = data.name
        if data.description is not None:
            version.description = data.description
        if data.status is not None:
            version.status = data.status
        if data.effective_date is not None:
            version.effective_date = data.effective_date
        if data.sharing is not None:
            version.sharing = data.sharing
        if data.wiki_page_title is not None:
            version.wiki_page_title = data.wiki_page_title
        session.add(version)
        await session.flush()
        return version

    async def delete(
        self,
        session: AsyncSession,
        version: Version,
    ) -> None:
        """Delete *version* (issues are SET NULL on fixed_version_id)."""
        await session.delete(version)
        await session.flush()

    # -----------------------------------------------------------------------
    # Admin: cross-project listing
    # -----------------------------------------------------------------------

    async def list_all(
        self,
        session: AsyncSession,
    ) -> list[dict]:
        """List all versions across all projects with issue counts (admin only).

        Returns a list of dicts with version fields, project key/name, and
        open/closed issue counts suitable for the admin versions page.
        """
        # Fetch all versions with their project info
        stmt = (
            select(Version, Project.key, Project.name.label("project_name"))
            .join(Project, Project.id == Version.project_id)
            .order_by(Project.name.asc(), Project.id.asc())
        )
        fetched = (await session.execute(stmt)).all()
        # Keep the project grouping from SQL and order each project's versions
        # with the shared display order (natural names, collation-independent).
        rows: list = []
        group: list = []
        for row in fetched:
            if group and group[0][0].project_id != row[0].project_id:
                rows.extend(order_versions(group, attrs=lambda r: _version_attrs(r[0])))
                group = []
            group.append(row)
        rows.extend(order_versions(group, attrs=lambda r: _version_attrs(r[0])))

        if not rows:
            return []

        version_ids = [row[0].id for row in rows]

        # Aggregate open / closed counts
        counts_stmt = (
            select(
                Issue.fixed_version_id,
                func.count().label("total"),
                func.sum(case((IssueStatus.category.in_(["done", "closed"]), 1), else_=0)).label("closed_count"),
            )
            .join(IssueStatus, IssueStatus.id == Issue.status_id)
            .where(Issue.fixed_version_id.in_(version_ids))
            .group_by(Issue.fixed_version_id)
        )
        count_rows = (await session.execute(counts_stmt)).all()
        counts: dict[int, dict] = {
            row.fixed_version_id: {
                "total": row.total,
                "closed_count": int(row.closed_count or 0),
            }
            for row in count_rows
        }

        result: list[dict] = []
        for version, project_key, project_name in rows:
            c = counts.get(version.id, {"total": 0, "closed_count": 0})
            total = c["total"]
            closed = c["closed_count"]
            open_count = total - closed
            progress = int(closed / total * 100) if total > 0 else 0
            overdue = (
                version.effective_date is not None
                and version.status != "closed"
                and version.effective_date < datetime.date.today()
            )

            result.append(
                {
                    "id": version.id,
                    "name": version.name,
                    "project_key": project_key,
                    "project_name": project_name,
                    "status": version.status,
                    "due_date": version.effective_date.isoformat() if version.effective_date else None,
                    "progress": progress,
                    "open_count": open_count,
                    "closed_count": closed,
                    "overdue": overdue,
                }
            )
        return result

    # -----------------------------------------------------------------------
    # Roadmap
    # -----------------------------------------------------------------------

    async def roadmap(
        self,
        session: AsyncSession,
        project: Project,
        reader: User,
        sort: str | None = None,
    ) -> list[RoadmapEntry]:
        """Return roadmap data for *project*: versions with open/closed issue counts.

        Counts cover only the issues *reader* may see
        (``IssueService.visible_issues_clause``, which also applies an API
        key's project limit), so progress never reveals hidden issues. The
        clause is cross-project because a shared version can hold issues of
        other projects.

        Only versions belonging to the project are included. Version sharing
        is not shown on the roadmap; ``visible_versions`` is the reader-aware
        query for it.

        Each entry carries:
        - ``open_count``  — issues whose status is NOT closed
        - ``closed_count`` — issues whose status is done or closed
        - ``progress_percent`` — closed / total * 100 (0 if total == 0)

        Entries follow :func:`order_versions` with *sort* (default: open and
        locked by due date, then closed).
        """
        versions = await self.list_for_project(session, project.id, sort)
        if not versions:
            return []

        version_ids = [v.id for v in versions]

        # Aggregate open / closed counts in a single query
        # done + closed categories count toward progress
        stmt = (
            select(
                Issue.fixed_version_id,
                func.count().label("total"),
                func.sum(case((IssueStatus.category.in_(["done", "closed"]), 1), else_=0)).label("closed_count"),
            )
            .join(IssueStatus, IssueStatus.id == Issue.status_id)
            .where(Issue.fixed_version_id.in_(version_ids))
            .group_by(Issue.fixed_version_id)
        )
        visibility = await IssueService().visible_issues_clause(session, reader)
        if visibility is not None:
            stmt = stmt.where(visibility)

        rows = (await session.execute(stmt)).all()
        counts: dict[int, dict] = {
            row.fixed_version_id: {
                "total": row.total,
                "closed_count": int(row.closed_count or 0),
            }
            for row in rows
        }

        # Need project keys to build VersionOut — fetch project key map
        proj_result = await session.execute(select(Project.id, Project.key).where(Project.id == project.id))
        project_key_map = {row.id: row.key for row in proj_result.all()}

        entries: list[RoadmapEntry] = []
        for version in versions:
            c = counts.get(version.id, {"total": 0, "closed_count": 0})
            total = c["total"]
            closed = c["closed_count"]
            open_count = total - closed
            progress = int(closed / total * 100) if total > 0 else 0
            proj_key = project_key_map.get(version.project_id, project.key)

            version_out = VersionOut(
                id=version.id,
                name=version.name,
                description=version.description,
                status=version.status,
                effective_date=version.effective_date,
                sharing=version.sharing,
                wiki_page_title=version.wiki_page_title,
                project_key=proj_key,
                created_at=version.created_at,
            )
            entries.append(
                RoadmapEntry(
                    version=version_out,
                    open_count=open_count,
                    closed_count=closed,
                    total=total,
                    progress_percent=progress,
                )
            )
        return entries

    # -----------------------------------------------------------------------
    # Sharing / visibility
    # -----------------------------------------------------------------------

    async def visible_versions(
        self,
        session: AsyncSession,
        project: Project,
        reader: User,
    ) -> list[Version]:
        """Return the versions *reader* may see from *project*, respecting sharing levels.

        Sharing semantics
        -----------------
        none        — only versions whose project_id == project.id
        descendants — own project + all descendant projects (ltree <@ path)
        hierarchy   — all projects in the ancestor–descendant chain (path @> or <@)
        tree        — the whole project tree (root + all descendants)
        system      — every project

        Sharing decides which projects a version reaches; it never lets a
        reader see a version whose owning project they cannot open. Versions of
        such a project (private without membership, or anything not readable
        anonymously) are left out, using the same predicate as the project
        list. Admins get no predicate and see every shared version.
        """
        from specivo.services.project_service import ProjectService

        own_path = project.path

        # Fetch candidate versions with their owning project's path, then
        # filter by sharing rule. For large installations the sharing rules
        # could move into SQL too, but this is clear and correct for the
        # current scale.
        stmt = select(Version, Project.path).join(Project, Project.id == Version.project_id)
        accessible = await ProjectService().accessible_projects_clause(session, reader)
        if accessible is not None:
            stmt = stmt.where(accessible)
        rows = (await session.execute(stmt)).all()

        visible: list[Version] = []
        for version, v_path in rows:
            sharing = version.sharing

            if sharing == "system":
                visible.append(version)
                continue

            if version.project_id == project.id:
                visible.append(version)
                continue

            if sharing == "none":
                continue

            if sharing == "descendants":
                # Version shared with descendants: visible to the owning project
                # and all projects below it in the hierarchy (own_path is a
                # descendant of v_path, i.e. own_path starts with v_path + ".").
                if own_path.startswith(v_path + "."):
                    visible.append(version)

            elif sharing == "hierarchy":
                # Version's project is an ancestor or descendant of this project
                if own_path.startswith(v_path + ".") or v_path.startswith(own_path + "."):
                    visible.append(version)

            elif sharing == "tree":
                # Find the root of both projects and check they share the same root
                own_root = own_path.split(".")[0]
                v_root = v_path.split(".")[0]
                if own_root == v_root:
                    visible.append(version)

        return visible
