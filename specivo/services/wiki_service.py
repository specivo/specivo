"""Wiki service — page CRUD, content versioning, redirects."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.core.exceptions import AppError, ConflictError, NotFoundError
from specivo.models.user import User
from specivo.models.wiki import Wiki, WikiContent, WikiPage, WikiRedirect
from specivo.services.wiki_utils import slugify as _slugify

logger = logging.getLogger(__name__)


class WikiService:
    """Service layer for wiki operations."""

    async def get_wiki(self, session: AsyncSession, project_id: int) -> Wiki | None:
        """Get existing wiki for the project, or return None."""
        return await self._get_wiki(session, project_id)

    async def get_or_create_wiki(self, session: AsyncSession, project_id: int) -> Wiki:
        """Get existing wiki for the project, or create one."""
        stmt = select(Wiki).where(Wiki.project_id == project_id)
        result = await session.execute(stmt)
        wiki = result.scalar_one_or_none()
        if wiki is not None:
            return wiki

        wiki = Wiki(project_id=project_id)
        session.add(wiki)
        await session.flush()
        return wiki

    async def create_page(
        self,
        session: AsyncSession,
        project_id: int,
        title: str,
        text: str,
        author: User,
        parent_slug: str | None = None,
        comments: str | None = None,
    ) -> tuple[WikiPage, WikiContent]:
        """Create a wiki page with initial content (version 1)."""
        wiki = await self.get_or_create_wiki(session, project_id)
        slug = _slugify(title)

        parent_id: int | None = None
        if parent_slug:
            parent_page = await self._get_page_by_slug(session, wiki.id, parent_slug)
            if parent_page is None:
                raise NotFoundError(f"Parent page '{parent_slug}' not found")
            parent_id = parent_page.id

        page = WikiPage(
            wiki_id=wiki.id,
            title=title,
            slug=slug,
            parent_id=parent_id,
        )
        session.add(page)

        try:
            await session.flush()
        except IntegrityError as exc:
            msg = str(exc.orig).lower() if exc.orig else ""
            if "uq_wiki_pages_wiki_slug" in msg:
                raise AppError(
                    code="conflict",
                    message=f"A page with slug '{slug}' already exists",
                    status_code=409,
                ) from exc
            raise

        content = WikiContent(
            page_id=page.id,
            author_id=author.id,
            text=text,
            version=1,
            comments=comments,
        )
        session.add(content)
        await session.flush()

        # Generate search embeddings (inline, non-blocking on failure)
        try:
            from specivo.services.chunking_service import ChunkingService
            from specivo.services.embedding_service import EmbeddingService

            chunks = ChunkingService().chunk_wiki_page(title, text)
            await EmbeddingService().embed_source(session, "wiki_page", page.id, project_id, chunks)
        except Exception:
            logger.debug("Embedding generation skipped for wiki page %s", slug)

        # Rebuild wiki link graph (async, non-blocking on failure)
        try:
            from specivo.tasks.wiki_links import rebuild_wiki_page_links

            rebuild_wiki_page_links.delay(wiki.id, page.id)
        except Exception:
            logger.debug("Link graph rebuild dispatch skipped")

        return page, content

    async def get_page(
        self,
        session: AsyncSession,
        project_id: int,
        slug: str,
    ) -> tuple[WikiPage, WikiContent]:
        """Get a page by slug, following redirects. Returns page + latest content."""
        wiki = await self._get_wiki(session, project_id)
        if wiki is None:
            raise NotFoundError(f"Wiki page '{slug}' not found")

        page = await self._get_page_by_slug(session, wiki.id, slug)

        # Follow redirect if page not found directly
        if page is None:
            redirect = await self._get_redirect(session, wiki.id, slug)
            if redirect is not None:
                page = await self._get_page_by_slug(session, wiki.id, redirect.redirected_to)

        if page is None:
            raise NotFoundError(f"Wiki page '{slug}' not found")

        content = await self._get_latest_content(session, page.id)
        if content is None:
            raise NotFoundError(f"Wiki page '{slug}' has no content")

        return page, content

    async def update_page(
        self,
        session: AsyncSession,
        page_id: int,
        text: str,
        author: User,
        lock_version: int,
        comment: str | None = None,
    ) -> tuple[WikiPage, WikiContent]:
        """Update page content, creating a new version."""
        page = await self._get_page_by_id(session, page_id)
        if page is None:
            raise NotFoundError("Wiki page not found")

        if page.lock_version != lock_version:
            raise ConflictError("Page has been modified by another user. Please refresh and try again.")

        # Get current max version
        stmt = select(func.max(WikiContent.version)).where(WikiContent.page_id == page.id)
        result = await session.execute(stmt)
        max_version = result.scalar_one_or_none() or 0

        content = WikiContent(
            page_id=page.id,
            author_id=author.id,
            text=text,
            version=max_version + 1,
            comments=comment,
        )
        session.add(content)

        # Bump lock_version via SQLAlchemy's version_id_col mechanism
        # We need to trigger an UPDATE on the page row
        page.title = page.title  # no-op assignment to mark dirty
        await session.flush()
        await session.refresh(page)

        # Rebuild wiki link graph (async, non-blocking on failure)
        try:
            from specivo.tasks.wiki_links import rebuild_wiki_page_links

            rebuild_wiki_page_links.delay(page.wiki_id, page.id)
        except Exception:
            logger.debug("Link graph rebuild dispatch skipped")

        return page, content

    async def delete_page(self, session: AsyncSession, page_id: int) -> None:
        """Delete a wiki page and all its content versions."""
        page = await self._get_page_by_id(session, page_id)
        if page is None:
            raise NotFoundError("Wiki page not found")
        await session.delete(page)
        await session.flush()

    async def list_pages(self, session: AsyncSession, project_id: int) -> list[WikiPage]:
        """List all pages for a project."""
        wiki = await self._get_wiki(session, project_id)
        if wiki is None:
            return []

        stmt = select(WikiPage).where(WikiPage.wiki_id == wiki.id).order_by(WikiPage.title)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_page_history(self, session: AsyncSession, page_id: int) -> list[WikiContent]:
        """Get all content versions for a page, newest first."""
        stmt = (
            select(WikiContent)
            .where(WikiContent.page_id == page_id)
            .options(selectinload(WikiContent.author))
            .order_by(WikiContent.version.desc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_page_version(self, session: AsyncSession, page_id: int, version: int) -> WikiContent:
        """Get a specific content version."""
        stmt = (
            select(WikiContent)
            .where(WikiContent.page_id == page_id, WikiContent.version == version)
            .options(selectinload(WikiContent.author))
        )
        result = await session.execute(stmt)
        content = result.scalar_one_or_none()
        if content is None:
            raise NotFoundError(f"Version {version} not found")
        return content

    async def rename_page(
        self,
        session: AsyncSession,
        page_id: int,
        new_title: str,
        lock_version: int,
    ) -> WikiPage:
        """Rename a page, creating a redirect from the old slug."""
        page = await self._get_page_by_id(session, page_id)
        if page is None:
            raise NotFoundError("Wiki page not found")

        if page.lock_version != lock_version:
            raise ConflictError("Page has been modified by another user. Please refresh and try again.")

        old_slug = page.slug
        new_slug = _slugify(new_title)

        # Create redirect from old slug
        redirect = WikiRedirect(
            wiki_id=page.wiki_id,
            title_from=old_slug,
            redirected_to=new_slug,
        )
        session.add(redirect)

        page.title = new_title
        page.slug = new_slug

        try:
            await session.flush()
        except IntegrityError as exc:
            msg = str(exc.orig).lower() if exc.orig else ""
            if "uq_wiki_pages_wiki_slug" in msg:
                raise AppError(
                    code="conflict",
                    message=f"A page with slug '{new_slug}' already exists",
                    status_code=409,
                ) from exc
            raise

        await session.refresh(page)
        return page

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    async def _get_wiki(self, session: AsyncSession, project_id: int) -> Wiki | None:
        stmt = select(Wiki).where(Wiki.project_id == project_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_page_by_slug(self, session: AsyncSession, wiki_id: int, slug: str) -> WikiPage | None:
        stmt = select(WikiPage).where(WikiPage.wiki_id == wiki_id, WikiPage.slug == slug)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_page_by_id(self, session: AsyncSession, page_id: int) -> WikiPage | None:
        stmt = select(WikiPage).where(WikiPage.id == page_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_latest_content(self, session: AsyncSession, page_id: int) -> WikiContent | None:
        stmt = (
            select(WikiContent)
            .where(WikiContent.page_id == page_id)
            .options(selectinload(WikiContent.author))
            .order_by(WikiContent.version.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_redirect(self, session: AsyncSession, wiki_id: int, slug: str) -> WikiRedirect | None:
        stmt = select(WikiRedirect).where(WikiRedirect.wiki_id == wiki_id, WikiRedirect.title_from == slug)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
