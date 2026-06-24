"""Wiki service — page CRUD, content versioning, redirects."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.core.exceptions import AppError, ConflictError, NotFoundError, ValidationError
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

        try:
            wiki = Wiki(project_id=project_id)
            session.add(wiki)
            await session.flush()
            return wiki
        except IntegrityError:
            await session.rollback()
            result = await session.execute(stmt)
            wiki = result.scalar_one_or_none()
            if wiki is not None:
                return wiki
            raise

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

        # Titles with no Latin/digit characters (e.g. Thai-only) slugify to
        # an empty string, which is neither addressable nor unique. Insert with a
        # throwaway unique placeholder so the row flushes without colliding, then
        # swap in a stable, unique, id-based slug once the id is assigned.
        page = WikiPage(
            wiki_id=wiki.id,
            title=title,
            slug=slug or f"tmp-{uuid.uuid4().hex}",
            parent_id=parent_id,
        )
        session.add(page)

        try:
            await session.flush()
        except IntegrityError as exc:
            msg = str(exc.orig).lower() if exc.orig else ""
            if "uq_wiki_pages_wiki_slug" in msg or "uq_wiki_pages_wiki_slug_active" in msg:
                raise AppError(
                    code="conflict",
                    message=f"A page with slug '{slug}' already exists",
                    status_code=409,
                ) from exc
            raise

        if not slug:
            slug = page.slug = f"page-{page.id}"
            await session.flush()
            # The UPDATE expires server-side columns (e.g. updated_at); reload so
            # the caller can serialize the page without triggering lazy IO.
            await session.refresh(page)

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
            from specivo.schemas.search import SearchSourceType
            from specivo.services.chunking_service import ChunkingService
            from specivo.services.embedding_service import EmbeddingService

            chunks = ChunkingService().chunk_wiki_page(title, text)
            await EmbeddingService().embed_source(
                session, SearchSourceType.WIKI_PAGE, page.id, project_id, chunks
            )
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
        """Get a page by slug, following redirects. Returns page + latest content.

        The slug is normalized (lowercased, underscores → hyphens) so callers
        can pass titles like ``GDB_Illusion_of_Choice`` and still find the page.
        """
        slug = _slugify(slug)
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

    async def ensure_home_page(self, session: AsyncSession, project_id: int, author: User) -> WikiPage:
        """Ensure the Home page exists for the project's wiki.

        Auto-creates it if missing. Returns the Home page.
        """
        wiki = await self.get_or_create_wiki(session, project_id)
        home = await self._get_page_by_slug(session, wiki.id, "home")
        if home is not None:
            return home

        page, _content = await self.create_page(
            session,
            project_id,
            "Home",
            "Welcome to the wiki. Edit this page to get started.",
            author,
            comments="Auto-created home page",
        )
        return page

    async def delete_page(
        self,
        session: AsyncSession,
        page_id: int,
        deleted_by: User,
        *,
        cascade_children: bool = False,
    ) -> list[int]:
        """Soft-delete a wiki page.

        The Home page (slug='home') cannot be deleted.
        If cascade_children is True, all descendants are also soft-deleted.
        If False, children are re-parented to the deleted page's parent.

        Returns list of soft-deleted page IDs.
        """
        page = await self._get_page_by_id(session, page_id)
        if page is None:
            raise NotFoundError("Wiki page not found")
        if page.slug == "home":
            raise ValidationError("The Home page cannot be deleted")

        now = datetime.now(UTC)
        deleted_ids: list[int] = [page.id]

        page.deleted_at = now
        page.deleted_by_id = deleted_by.id

        if cascade_children:
            # Recursively find all descendants
            ids_to_check = {page.id}
            all_descendant_ids: set[int] = set()
            while ids_to_check:
                stmt = select(WikiPage.id).where(
                    WikiPage.parent_id.in_(ids_to_check),
                    WikiPage.deleted_at.is_(None),
                )
                result = await session.execute(stmt)
                child_ids = {row[0] for row in result.all()}
                new_ids = child_ids - all_descendant_ids
                all_descendant_ids.update(new_ids)
                ids_to_check = new_ids

            if all_descendant_ids:
                await session.execute(
                    update(WikiPage)
                    .where(WikiPage.id.in_(all_descendant_ids))
                    .values(deleted_at=now, deleted_by_id=deleted_by.id)
                )
                deleted_ids.extend(all_descendant_ids)
        else:
            # Re-parent children to the deleted page's parent
            await session.execute(
                update(WikiPage)
                .where(WikiPage.parent_id == page.id, WikiPage.deleted_at.is_(None))
                .values(parent_id=page.parent_id)
            )

        await session.flush()

        # Remove search index for all deleted pages
        try:
            from specivo.schemas.search import SearchSourceType
            from specivo.services.embedding_service import EmbeddingService

            embed_svc = EmbeddingService()
            for pid in deleted_ids:
                await embed_svc.remove_source(session, SearchSourceType.WIKI_PAGE, pid)
        except Exception:
            logger.debug("Search index cleanup skipped for wiki page delete %d", page.id)

        # Audit log
        try:
            from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

            await SecurityAuditService().log_event(
                session=session,
                event_type=AuditEvent.WIKI_DELETED,
                user_id=deleted_by.id,
                resource_type="WikiPage",
                resource_id=page.id,
                project_id=None,
                details={
                    "slug": page.slug,
                    "title": page.title,
                    "cascade_children": cascade_children,
                    "deleted_page_ids": deleted_ids,
                },
            )
        except Exception:
            logger.debug("Audit logging skipped for wiki page delete %d", page.id)

        return deleted_ids

    async def restore_page(
        self,
        session: AsyncSession,
        page_id: int,
        *,
        cascade: bool = True,
    ) -> list[int]:
        """Restore a soft-deleted wiki page.

        If cascade is True, co-deleted children (same deleted_at ±1s) are also restored.
        Raises ConflictError if an active page with the same slug exists.

        Returns list of restored page IDs.
        """
        page = await self._get_page_by_id_include_deleted(session, page_id)
        if page is None or page.deleted_at is None:
            raise NotFoundError("Deleted wiki page not found")

        # Check slug conflict
        existing = await self._get_page_by_slug(session, page.wiki_id, page.slug)
        if existing is not None:
            raise ConflictError(f"A page with slug '{page.slug}' already exists")

        restored_ids: list[int] = [page.id]
        deleted_at = page.deleted_at
        deleted_by_id = page.deleted_by_id

        page.deleted_at = None
        page.deleted_by_id = None

        if cascade:
            # Find co-deleted children (same timestamp ±1 second, same deleter)
            lower = deleted_at - timedelta(seconds=1)
            upper = deleted_at + timedelta(seconds=1)
            cascade_conditions = [
                WikiPage.wiki_id == page.wiki_id,
                WikiPage.id != page.id,
                WikiPage.deleted_at.isnot(None),
                WikiPage.deleted_at >= lower,
                WikiPage.deleted_at <= upper,
            ]
            if deleted_by_id is not None:
                cascade_conditions.append(WikiPage.deleted_by_id == deleted_by_id)
            stmt = select(WikiPage).where(*cascade_conditions)
            result = await session.execute(stmt)
            co_deleted = list(result.scalars().all())
            for child in co_deleted:
                child.deleted_at = None
                child.deleted_by_id = None
                restored_ids.append(child.id)

        await session.flush()

        # Audit log
        try:
            from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

            await SecurityAuditService().log_event(
                session=session,
                event_type=AuditEvent.WIKI_RESTORED,
                user_id=None,
                resource_type="WikiPage",
                resource_id=page.id,
                project_id=None,
                details={
                    "slug": page.slug,
                    "title": page.title,
                    "cascade": cascade,
                    "restored_page_ids": restored_ids,
                },
            )
        except Exception:
            logger.debug("Audit logging skipped for wiki page restore %d", page.id)

        # Re-index restored pages for search
        try:
            from specivo.schemas.search import SearchSourceType
            from specivo.services.chunking_service import ChunkingService
            from specivo.services.embedding_service import EmbeddingService

            wiki = await self._get_wiki_by_id(session, page.wiki_id)
            project_id = wiki.project_id if wiki else None
            if project_id:
                embed_svc = EmbeddingService()
                chunk_svc = ChunkingService()
                for pid in restored_ids:
                    restored_page = await self._get_page_by_id(session, pid)
                    if restored_page:
                        content = await self._get_latest_content(session, pid)
                        if content:
                            chunks = chunk_svc.chunk_wiki_page(restored_page.title, content.text)
                            await embed_svc.embed_source(
                                session, SearchSourceType.WIKI_PAGE, pid, project_id, chunks
                            )
        except Exception:
            logger.debug("Search re-indexing skipped for wiki page restore %d", page.id)

        return restored_ids

    async def list_deleted_pages(self, session: AsyncSession, project_id: int) -> list[WikiPage]:
        """List all soft-deleted pages for a project (trash view)."""
        wiki = await self._get_wiki(session, project_id)
        if wiki is None:
            return []

        stmt = (
            select(WikiPage)
            .where(WikiPage.wiki_id == wiki.id, WikiPage.deleted_at.isnot(None))
            .order_by(WikiPage.deleted_at.desc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def hard_delete_expired_pages(
        self, session: AsyncSession, retention_days: int = 90,
    ) -> list[int]:
        """Permanently delete pages in trash older than retention_days.

        Returns list of hard-deleted page IDs.
        """
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        # First collect IDs for the return value
        stmt = select(WikiPage.id).where(
            WikiPage.deleted_at.isnot(None),
            WikiPage.deleted_at < cutoff,
        )
        result = await session.execute(stmt)
        expired_ids = [row[0] for row in result.all()]

        if expired_ids:
            await session.execute(
                delete(WikiPage).where(WikiPage.id.in_(expired_ids))
            )
            await session.flush()

        return expired_ids

    async def list_pages(self, session: AsyncSession, project_id: int) -> list[WikiPage]:
        """List all pages for a project."""
        wiki = await self._get_wiki(session, project_id)
        if wiki is None:
            return []

        stmt = (
            select(WikiPage)
            .where(WikiPage.wiki_id == wiki.id, WikiPage.deleted_at.is_(None))
            .order_by(WikiPage.title)
        )
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
        # A non-Latin title slugifies to an empty string; fall back to a stable,
        # unique, id-based slug so the page is never left unaddressable.
        new_slug = _slugify(new_title) or f"page-{page.id}"

        # Create a redirect from the old slug — but never a self-redirect, which
        # the id-based fallback can produce when the slug is unchanged.
        if new_slug != old_slug:
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
            if "uq_wiki_pages_wiki_slug" in msg or "uq_wiki_pages_wiki_slug_active" in msg:
                raise AppError(
                    code="conflict",
                    message=f"A page with slug '{new_slug}' already exists",
                    status_code=409,
                ) from exc
            raise

        await session.refresh(page)
        return page

    @staticmethod
    def build_page_tree(pages: list[WikiPage]) -> dict[int | None, list[WikiPage]]:
        """Group pages by parent_id for tree rendering."""
        tree: dict[int | None, list[WikiPage]] = {}
        for p in pages:
            tree.setdefault(p.parent_id, []).append(p)
        return tree

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    async def _get_wiki(self, session: AsyncSession, project_id: int) -> Wiki | None:
        stmt = select(Wiki).where(Wiki.project_id == project_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_wiki_by_id(self, session: AsyncSession, wiki_id: int) -> Wiki | None:
        stmt = select(Wiki).where(Wiki.id == wiki_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_page_by_slug(self, session: AsyncSession, wiki_id: int, slug: str) -> WikiPage | None:
        stmt = select(WikiPage).where(
            WikiPage.wiki_id == wiki_id, WikiPage.slug == slug, WikiPage.deleted_at.is_(None),
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_page_by_id(self, session: AsyncSession, page_id: int) -> WikiPage | None:
        stmt = select(WikiPage).where(WikiPage.id == page_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_page_by_id_include_deleted(self, session: AsyncSession, page_id: int) -> WikiPage | None:
        """Like _get_page_by_id but does NOT filter out soft-deleted pages."""
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
