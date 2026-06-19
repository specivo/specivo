"""Tag service — per-project tag vocabulary and entity tagging.

Tags are flat, case-insensitively-unique labels scoped to a project, applied
to issues and wiki pages via :class:`specivo.models.tag.TagLink`. Members may
create tags on the fly while applying them; vocabulary curation (rename,
recolor, delete) is gated at the API/MCP layer.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import ConflictError, NotFoundError
from specivo.models.member import Member
from specivo.models.project import Project
from specivo.models.tag import Tag, TagLink
from specivo.models.user import User
from specivo.schemas.tag import TagCreate, TagUpdate

logger = logging.getLogger(__name__)


class TagService:
    """Service layer for Tag operations."""

    # -----------------------------------------------------------------------
    # Vocabulary lookups
    # -----------------------------------------------------------------------

    async def get_by_id(self, session: AsyncSession, tag_id: int) -> Tag:
        """Return a Tag by PK; raises NotFoundError if missing."""
        result = await session.execute(select(Tag).where(Tag.id == tag_id))
        tag = result.scalar_one_or_none()
        if tag is None:
            raise NotFoundError(f"Tag {tag_id} not found")
        return tag

    async def get_by_name(self, session: AsyncSession, project_id: int, name: str) -> Tag | None:
        """Case-insensitive lookup of a tag by name within a project."""
        result = await session.execute(
            select(Tag).where(
                Tag.project_id == project_id,
                func.lower(Tag.name) == name.strip().lower(),
            )
        )
        return result.scalar_one_or_none()

    async def list_for_project(self, session: AsyncSession, project_id: int) -> list[Tag]:
        """List tags for a project, ordered case-insensitively by name."""
        stmt = select(Tag).where(Tag.project_id == project_id).order_by(func.lower(Tag.name))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def list_with_usage(self, session: AsyncSession, project_id: int) -> list[tuple[Tag, int, int]]:
        """List tags with (issue_count, wiki_count) usage, ordered by name."""
        stmt = (
            select(
                Tag,
                func.count(TagLink.issue_id).label("issue_count"),
                func.count(TagLink.wiki_page_id).label("wiki_count"),
            )
            .outerjoin(TagLink, TagLink.tag_id == Tag.id)
            .where(Tag.project_id == project_id)
            .group_by(Tag.id)
            .order_by(func.lower(Tag.name))
        )
        rows = (await session.execute(stmt)).all()
        return [(row[0], int(row[1] or 0), int(row[2] or 0)) for row in rows]

    async def search_for_project(
        self,
        session: AsyncSession,
        project_id: int,
        query: str | None = None,
        limit: int = 20,
    ) -> list[Tag]:
        """Autocomplete search for tags.

        Empty query returns the first ``limit`` tags ordered by name; a
        non-empty query performs a case-insensitive substring match.
        """
        stmt = select(Tag).where(Tag.project_id == project_id)
        q = (query or "").strip()
        if q:
            stmt = stmt.where(Tag.name.ilike(f"%{q}%"))
        stmt = stmt.order_by(func.lower(Tag.name)).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def search_across_projects(
        self,
        session: AsyncSession,
        user: User,
        query: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Autocomplete tag names across every project the *user* can access.

        Deduplicated case-insensitively by name (one row per distinct lowercased
        name). Admins see all tags; other users see tags in projects they are a
        member of or that are public. Returns ``[{id, name, color}]`` ordered by
        name.
        """
        stmt = select(Tag)
        if not user.is_admin:
            member_projects = select(Member.project_id).where(Member.user_id == user.id).scalar_subquery()
            public_projects = select(Project.id).where(Project.is_public.is_(True)).scalar_subquery()
            stmt = stmt.where(or_(Tag.project_id.in_(member_projects), Tag.project_id.in_(public_projects)))
        q = (query or "").strip()
        if q:
            stmt = stmt.where(Tag.name.ilike(f"%{q}%"))
        # DISTINCT ON (lower(name)) collapses the same name across projects; the
        # leading ORDER BY lower(name) is required for DISTINCT ON.
        stmt = stmt.distinct(func.lower(Tag.name)).order_by(func.lower(Tag.name), Tag.id).limit(limit)
        result = await session.execute(stmt)
        return [{"id": t.id, "name": t.name, "color": t.color} for t in result.scalars().all()]

    # -----------------------------------------------------------------------
    # Vocabulary CRUD
    # -----------------------------------------------------------------------

    async def create(self, session: AsyncSession, project: Project, data: TagCreate) -> Tag:
        """Create a tag for *project*; raises ConflictError on duplicate name."""
        name = data.name.strip()
        existing = await self.get_by_name(session, project.id, name)
        if existing is not None:
            raise ConflictError(message=f"A tag named '{name}' already exists in this project")
        tag = Tag(project_id=project.id, name=name, color=data.color)
        session.add(tag)
        await session.flush()
        return tag

    async def get_or_create(
        self,
        session: AsyncSession,
        project_id: int,
        name: str,
        user: User,
        color: str | None = None,
    ) -> tuple[Tag, bool]:
        """Return ``(tag, created)`` for *name*, creating it if absent."""
        name = name.strip()
        if not name:
            raise ConflictError(message="Tag name must not be empty")
        existing = await self.get_by_name(session, project_id, name)
        if existing is not None:
            return existing, False
        tag = Tag(project_id=project_id, name=name, color=color, created_by_id=user.id)
        session.add(tag)
        await session.flush()
        return tag, True

    async def update(self, session: AsyncSession, tag: Tag, data: TagUpdate) -> Tag:
        """Apply a partial update (rename / recolor) to *tag*."""
        if data.name is not None:
            new_name = data.name.strip()
            clash = await self.get_by_name(session, tag.project_id, new_name)
            if clash is not None and clash.id != tag.id:
                raise ConflictError(message=f"A tag named '{new_name}' already exists in this project")
            tag.name = new_name
        if "color" in data.model_fields_set:
            tag.color = data.color
        session.add(tag)
        await session.flush()
        return tag

    async def delete(self, session: AsyncSession, tag: Tag) -> None:
        """Delete *tag*; CASCADE removes all its links."""
        await session.delete(tag)
        await session.flush()

    # -----------------------------------------------------------------------
    # Entity tagging — issues
    # -----------------------------------------------------------------------

    async def tags_for_issue(self, session: AsyncSession, issue_id: int) -> list[Tag]:
        stmt = (
            select(Tag)
            .join(TagLink, TagLink.tag_id == Tag.id)
            .where(TagLink.issue_id == issue_id)
            .order_by(func.lower(Tag.name))
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def add_to_issue(
        self, session: AsyncSession, project: Project, issue_id: int, name: str, user: User
    ) -> tuple[Tag, bool]:
        """Apply tag *name* to an issue (idempotent). Returns ``(tag, link_created)``."""
        tag, _ = await self.get_or_create(session, project.id, name, user)
        existing = await session.execute(
            select(TagLink.id).where(TagLink.tag_id == tag.id, TagLink.issue_id == issue_id)
        )
        if existing.scalar_one_or_none() is not None:
            return tag, False
        session.add(TagLink(tag_id=tag.id, issue_id=issue_id, created_by_id=user.id))
        await session.flush()
        return tag, True

    async def remove_from_issue(self, session: AsyncSession, issue_id: int, tag_id: int) -> bool:
        """Detach a tag from an issue. Returns True if a link was removed."""
        result = await session.execute(select(TagLink).where(TagLink.tag_id == tag_id, TagLink.issue_id == issue_id))
        link = result.scalar_one_or_none()
        if link is None:
            return False
        await session.delete(link)
        await session.flush()
        return True

    async def set_issue_tags(
        self, session: AsyncSession, project: Project, issue_id: int, names: list[str], user: User
    ) -> dict[str, list[str]]:
        """Replace the full tag set on an issue. Returns ``{added, removed}`` names."""
        current = await self.tags_for_issue(session, issue_id)
        current_by_lower = {t.name.lower(): t for t in current}
        target_lowers: dict[str, str] = {}
        for raw in names:
            n = raw.strip()
            if n:
                target_lowers.setdefault(n.lower(), n)

        added: list[str] = []
        removed: list[str] = []

        for lower, original in target_lowers.items():
            if lower not in current_by_lower:
                tag, _ = await self.add_to_issue(session, project, issue_id, original, user)
                added.append(tag.name)

        for lower, tag in current_by_lower.items():
            if lower not in target_lowers:
                await self.remove_from_issue(session, issue_id, tag.id)
                removed.append(tag.name)

        return {"added": added, "removed": removed}

    async def bulk_add_to_issues(
        self, session: AsyncSession, project: Project, issue_ids: list[int], name: str, user: User
    ) -> int:
        """Apply tag *name* to many issues. Returns the number of new links."""
        if not issue_ids:
            return 0
        tag, _ = await self.get_or_create(session, project.id, name, user)
        existing = await session.execute(
            select(TagLink.issue_id).where(TagLink.tag_id == tag.id, TagLink.issue_id.in_(issue_ids))
        )
        already = {row[0] for row in existing.all()}
        created = 0
        for issue_id in issue_ids:
            if issue_id not in already:
                session.add(TagLink(tag_id=tag.id, issue_id=issue_id, created_by_id=user.id))
                created += 1
        await session.flush()
        return created

    async def bulk_remove_from_issues(self, session: AsyncSession, issue_ids: list[int], tag_id: int) -> int:
        """Detach a tag from many issues. Returns the number of removed links."""
        if not issue_ids:
            return 0
        result = await session.execute(select(TagLink).where(TagLink.tag_id == tag_id, TagLink.issue_id.in_(issue_ids)))
        links = list(result.scalars().all())
        for link in links:
            await session.delete(link)
        await session.flush()
        return len(links)

    # -----------------------------------------------------------------------
    # Entity tagging — wiki pages
    # -----------------------------------------------------------------------

    async def tags_for_wiki_page(self, session: AsyncSession, page_id: int) -> list[Tag]:
        stmt = (
            select(Tag)
            .join(TagLink, TagLink.tag_id == Tag.id)
            .where(TagLink.wiki_page_id == page_id)
            .order_by(func.lower(Tag.name))
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def add_to_wiki_page(
        self, session: AsyncSession, project: Project, page_id: int, name: str, user: User
    ) -> tuple[Tag, bool]:
        """Apply tag *name* to a wiki page (idempotent)."""
        tag, _ = await self.get_or_create(session, project.id, name, user)
        existing = await session.execute(
            select(TagLink.id).where(TagLink.tag_id == tag.id, TagLink.wiki_page_id == page_id)
        )
        if existing.scalar_one_or_none() is not None:
            return tag, False
        session.add(TagLink(tag_id=tag.id, wiki_page_id=page_id, created_by_id=user.id))
        await session.flush()
        return tag, True

    async def remove_from_wiki_page(self, session: AsyncSession, page_id: int, tag_id: int) -> bool:
        result = await session.execute(select(TagLink).where(TagLink.tag_id == tag_id, TagLink.wiki_page_id == page_id))
        link = result.scalar_one_or_none()
        if link is None:
            return False
        await session.delete(link)
        await session.flush()
        return True

    async def set_wiki_page_tags(
        self, session: AsyncSession, project: Project, page_id: int, names: list[str], user: User
    ) -> dict[str, list[str]]:
        """Replace the full tag set on a wiki page. Returns ``{added, removed}`` names."""
        current = await self.tags_for_wiki_page(session, page_id)
        current_by_lower = {t.name.lower(): t for t in current}
        target_lowers: dict[str, str] = {}
        for raw in names:
            n = raw.strip()
            if n:
                target_lowers.setdefault(n.lower(), n)

        added: list[str] = []
        removed: list[str] = []

        for lower, original in target_lowers.items():
            if lower not in current_by_lower:
                tag, _ = await self.add_to_wiki_page(session, project, page_id, original, user)
                added.append(tag.name)

        for lower, tag in current_by_lower.items():
            if lower not in target_lowers:
                await self.remove_from_wiki_page(session, page_id, tag.id)
                removed.append(tag.name)

        return {"added": added, "removed": removed}
