"""Version service — CRUD, roadmap, and sharing-aware visibility."""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError
from specivo.models.issue import Issue
from specivo.models.lookups import IssueStatus
from specivo.models.project import Project
from specivo.models.version import Version
from specivo.schemas.version import RoadmapEntry, VersionCreate, VersionOut, VersionUpdate

logger = logging.getLogger(__name__)


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
    ) -> list[Version]:
        """List versions for a project, ordered by effective_date nulls last, then name."""
        stmt = (
            select(Version)
            .where(Version.project_id == project_id)
            .order_by(Version.effective_date.asc().nullslast(), Version.name.asc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def update(
        self,
        session: AsyncSession,
        version: Version,
        data: VersionUpdate,
    ) -> Version:
        """Apply a partial update to *version*."""
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
            .order_by(Project.name.asc(), Version.effective_date.asc().nullslast(), Version.name.asc())
        )
        rows = (await session.execute(stmt)).all()

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
    ) -> list[RoadmapEntry]:
        """Return roadmap data for *project*: versions with open/closed issue counts.

        Only versions belonging to the project are included (visibility across
        projects via sharing is a separate query — see visible_versions).

        Each entry carries:
        - ``open_count``  — issues whose status is NOT closed
        - ``closed_count`` — issues whose status is done or closed
        - ``progress_percent`` — closed / total * 100 (0 if total == 0)
        """
        versions = await self.list_for_project(session, project.id)
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
    ) -> list[Version]:
        """Return all versions visible to *project*, respecting sharing levels.

        Sharing semantics
        -----------------
        none        — only versions whose project_id == project.id
        descendants — own project + all descendant projects (ltree <@ path)
        hierarchy   — all projects in the ancestor–descendant chain (path @> or <@)
        tree        — the whole project tree (root + all descendants)
        system      — every project
        """
        # Collect candidate version IDs by sharing type:
        # We do this in Python using ltree path comparisons via raw SQL.

        own_path = project.path

        # Fetch all versions, then filter by sharing rule.
        # For large installations a single JOIN query would be better, but
        # this is clear and correct for the current scale.

        all_versions_result = await session.execute(select(Version))
        all_versions = all_versions_result.scalars().all()

        if not all_versions:
            return []

        # Build project_id → path map for projects referenced by these versions
        project_ids = {v.project_id for v in all_versions}
        proj_stmt = select(Project.id, Project.path).where(Project.id.in_(project_ids))
        proj_rows = (await session.execute(proj_stmt)).all()
        path_map = {row.id: row.path for row in proj_rows}

        visible: list[Version] = []
        for version in all_versions:
            v_path = path_map.get(version.project_id, "")
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
