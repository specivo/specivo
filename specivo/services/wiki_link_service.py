"""WikiLinkService — parse [[wiki links]], store link graph, detect broken links."""

from __future__ import annotations

import html
import logging
import re

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.wiki import WikiContent, WikiPage, WikiPageLink
from specivo.services.wiki_utils import slugify

logger = logging.getLogger(__name__)

# Regex to find [[Target]] or [[Target|display text]] links
_WIKI_LINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")

# Safety cap to prevent pathological content from creating unbounded link rows
_MAX_LINKS_PER_PAGE = 500

# Batch size for resolving target slugs to page IDs
_SLUG_RESOLVE_BATCH_SIZE = 100


class WikiLinkService:
    """Service for wiki link graph operations."""

    def parse_links(self, text: str) -> list[tuple[str, str | None]]:
        """Parse ``[[Page_Name]]`` and ``[[Page_Name|display text]]`` from text.

        Returns a deduplicated list of ``(slug, display_text | None)`` tuples,
        preserving first-occurrence order. Results are capped at
        ``_MAX_LINKS_PER_PAGE`` unique links.
        """
        seen: set[str] = set()
        results: list[tuple[str, str | None]] = []

        for match in _WIKI_LINK_RE.finditer(text):
            raw_target = match.group(1).strip()
            display_text = match.group(2)
            if display_text is not None:
                display_text = html.escape(display_text.strip())

            if not raw_target:
                continue

            slug = slugify(raw_target)
            if not slug:
                continue

            if slug in seen:
                continue
            seen.add(slug)
            results.append((slug, display_text))

            if len(results) >= _MAX_LINKS_PER_PAGE:
                logger.warning(
                    "Link cap reached (%d): stopped parsing further links from page content",
                    _MAX_LINKS_PER_PAGE,
                )
                break

        return results

    async def rebuild_page_links(
        self,
        session: AsyncSession,
        wiki_id: int,
        page_id: int,
    ) -> int:
        """Delete old links for a page, parse latest content, and insert new links.

        Returns the number of links created.
        """
        # Get latest content for the page
        stmt = select(WikiContent).where(WikiContent.page_id == page_id).order_by(WikiContent.version.desc()).limit(1)
        result = await session.execute(stmt)
        content = result.scalar_one_or_none()
        if content is None:
            return 0

        # Parse links from content
        parsed = self.parse_links(content.text)

        # Delete existing links for this page
        await session.execute(delete(WikiPageLink).where(WikiPageLink.source_page_id == page_id))

        if not parsed:
            return 0

        # Batch-resolve target slugs to page IDs (chunked to avoid huge IN clauses)
        target_slugs = [slug for slug, _ in parsed]
        slug_to_page: dict[str, WikiPage] = {}
        for i in range(0, len(target_slugs), _SLUG_RESOLVE_BATCH_SIZE):
            batch = target_slugs[i : i + _SLUG_RESOLVE_BATCH_SIZE]
            resolve_stmt = select(WikiPage).where(
                WikiPage.wiki_id == wiki_id,
                WikiPage.slug.in_(batch),
            )
            resolve_result = await session.execute(resolve_stmt)
            for p in resolve_result.scalars().all():
                slug_to_page[p.slug] = p

        # Bulk insert new links
        new_links = []
        for slug, display_text in parsed:
            target_page = slug_to_page.get(slug)
            new_links.append(
                WikiPageLink(
                    wiki_id=wiki_id,
                    source_page_id=page_id,
                    target_page_id=target_page.id if target_page else None,
                    target_slug=slug,
                    display_text=display_text,
                )
            )
        session.add_all(new_links)

        await session.flush()
        logger.debug("Rebuilt %d link(s) for page %d in wiki %d", len(parsed), page_id, wiki_id)
        return len(parsed)

    async def resolve_incoming_links(
        self,
        session: AsyncSession,
        wiki_id: int,
        page_id: int,
        page_slug: str,
    ) -> int:
        """Fix broken links from other pages that point to this page's slug.

        When a page is created or renamed, other pages may have broken links
        (target_page_id IS NULL) with a target_slug matching this page.
        This resolves them in a single UPDATE.

        Returns the number of links resolved.
        """
        stmt = (
            update(WikiPageLink)
            .where(
                WikiPageLink.wiki_id == wiki_id,
                WikiPageLink.target_slug == page_slug,
                WikiPageLink.target_page_id.is_(None),
            )
            .values(target_page_id=page_id)
        )
        result = await session.execute(stmt)
        count = result.rowcount
        if count:
            logger.debug("Resolved %d incoming link(s) to page %d (%s)", count, page_id, page_slug)
        return count

    async def get_link_graph(
        self,
        session: AsyncSession,
        wiki_id: int,
    ) -> dict:
        """Return the link graph for a wiki as ``{"nodes": [...], "edges": [...]}``.

        Nodes are all pages in the wiki. Edges are all links with
        ``is_broken`` flag indicating unresolved targets.
        """
        # Nodes: lightweight column select (no ORM identity map overhead)
        pages_stmt = select(WikiPage.id, WikiPage.slug, WikiPage.title).where(WikiPage.wiki_id == wiki_id)
        pages_result = await session.execute(pages_stmt)
        nodes = [{"id": r.id, "slug": r.slug, "title": r.title} for r in pages_result]

        # Edges: lightweight column select
        links_stmt = select(
            WikiPageLink.source_page_id,
            WikiPageLink.target_page_id,
            WikiPageLink.target_slug,
            WikiPageLink.display_text,
        ).where(WikiPageLink.wiki_id == wiki_id)
        links_result = await session.execute(links_stmt)
        edges = [
            {
                "source_page_id": r.source_page_id,
                "target_page_id": r.target_page_id,
                "target_slug": r.target_slug,
                "display_text": r.display_text,
                "is_broken": r.target_page_id is None,
            }
            for r in links_result
        ]

        return {"nodes": nodes, "edges": edges}

    async def get_broken_links(
        self,
        session: AsyncSession,
        wiki_id: int,
    ) -> list[WikiPageLink]:
        """Return all links in the wiki where the target page does not exist."""
        stmt = select(WikiPageLink).where(
            WikiPageLink.wiki_id == wiki_id,
            WikiPageLink.target_page_id.is_(None),
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())
