"""Load wiki pages with their full history, watchers and redirects.

Version 1 goes through ``WikiService.create_page`` so slug generation, the
per-wiki uniqueness rules and the page tree behave as they do everywhere else.
Later revisions are inserted directly: the service is built to take a page from
one version to the next as somebody edits it, and seeding a history that
already exists is not that. Each revision keeps its own author, comment and
date, so the page history reads the way it did in the source.

Redirects are imported too. Redmine records one whenever a page is renamed and
Specivo has the same table, so a link written against a page's old name keeps
working after the migration.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from specivo.importers.core.backdate import backdate
from specivo.importers.core.ir import EntityType, IRWikiPage
from specivo.importers.core.pipeline import PhaseContext
from specivo.importers.load.issue_loader import convert_markup
from specivo.importers.load.user_loader import IMPORT_ACCOUNT_STATE_KEY, ensure_import_account
from specivo.models.user import User
from specivo.models.watcher import Watcher
from specivo.models.wiki import WikiContent, WikiPage, WikiRedirect
from specivo.services.wiki_service import WikiService
from specivo.services.wiki_utils import slugify

logger = logging.getLogger(__name__)

_wiki_service = WikiService()


async def load_wiki_pages(ctx: PhaseContext) -> None:
    """Create every wiki page in the selected projects, parents before children."""
    await ensure_import_account(ctx)

    for project_ref in ctx.project_refs:
        project_id = await ctx.id_map.get(ctx.session, EntityType.PROJECT, project_ref)
        if project_id is None:
            continue

        async for ir in ctx.adapter.extract_wiki_pages(project_ref):
            if await ctx.id_map.get(ctx.session, EntityType.WIKI_PAGE, ir.source_ref):
                ctx.summary.record_skipped(EntityType.WIKI_PAGE)
                continue
            await _create_page(ctx, project_id, ir)


async def _create_page(ctx: PhaseContext, project_id: int, ir: IRWikiPage) -> WikiPage | None:
    """Create one page and replay its revisions in order."""
    if not ir.title:
        ctx.warn("Wiki page has no title; skipped", page=ir.source_ref)
        return None

    parent_slug = await _parent_slug(ctx, ir.parent_ref)
    if ir.parent_ref and parent_slug is None:
        ctx.warn("Wiki page's parent was not imported; imported at the top level", page=ir.title)

    revisions = ir.versions or []
    first = revisions[0] if revisions else None
    author = await _resolve_author(ctx, first.author_ref if first else None)

    page, content = await _wiki_service.create_page(
        ctx.session,
        project_id,
        ir.title,
        convert_markup(ctx, first.text if first else "") or "",
        author,
        parent_slug=parent_slug,
        comments=(first.comments if first else None),
        skip_search_index=True,
        skip_link_rebuild=True,
    )

    if ir.protected:
        page.protected = True
        await ctx.session.flush()

    if first is not None:
        await backdate(
            ctx.session,
            WikiContent,
            content.id,
            created_at=first.created_at,
            updated_at=first.created_at,
        )
        await ctx.id_map.put(ctx.session, EntityType.WIKI_VERSION, first.source_ref, "wiki_contents", content.id)

    await _replay_revisions(ctx, page, revisions[1:])

    await ctx.id_map.put(ctx.session, EntityType.WIKI_PAGE, ir.source_ref, "wiki_pages", page.id)
    ctx.summary.record_created(EntityType.WIKI_PAGE)
    ctx.tick()
    return page


async def _replay_revisions(ctx: PhaseContext, page: WikiPage, revisions: list) -> None:
    """Insert revisions 2..N directly, each keeping its own author and date.

    Version numbers are renumbered consecutively rather than copied. Redmine
    can leave gaps when a revision is deleted, and Specivo's history reads by
    position, so a gap would look like a missing revision.
    """
    next_version = 2
    for revision in revisions:
        if await ctx.id_map.get(ctx.session, EntityType.WIKI_VERSION, revision.source_ref):
            ctx.summary.record_skipped(EntityType.WIKI_VERSION)
            continue

        author = await _resolve_author(ctx, revision.author_ref)
        content = WikiContent(
            page_id=page.id,
            author_id=author.id,
            text=convert_markup(ctx, revision.text) or "",
            version=next_version,
            comments=(revision.comments or None),
        )
        ctx.session.add(content)
        await ctx.session.flush()
        await backdate(
            ctx.session,
            WikiContent,
            content.id,
            created_at=revision.created_at,
            updated_at=revision.created_at,
        )
        await ctx.id_map.put(ctx.session, EntityType.WIKI_VERSION, revision.source_ref, "wiki_contents", content.id)
        ctx.summary.record_created(EntityType.WIKI_VERSION)
        next_version += 1


async def load_wiki_watchers(ctx: PhaseContext) -> None:
    """Subscribe imported users to the wiki pages they watched."""
    reader = getattr(ctx.adapter, "extract_wiki_watchers", None)
    if reader is None:
        return

    for project_ref in ctx.project_refs:
        async for ir in reader(project_ref):
            page_id = await ctx.id_map.get(ctx.session, EntityType.WIKI_PAGE, ir.container_ref)
            user_id = await ctx.id_map.get(ctx.session, EntityType.USER, ir.user_ref)
            if page_id is None or user_id is None:
                continue

            existing = await ctx.session.execute(
                select(Watcher.id).where(Watcher.wiki_page_id == page_id, Watcher.user_id == user_id).limit(1)
            )
            if existing.scalar_one_or_none() is not None:
                ctx.summary.record_skipped(EntityType.WATCHER)
                continue

            ctx.session.add(Watcher(wiki_page_id=page_id, user_id=user_id))
            ctx.summary.record_created(EntityType.WATCHER)
            ctx.tick()
    await ctx.session.flush()


async def load_wiki_redirects(ctx: PhaseContext) -> None:
    """Recreate the redirects left behind by page renames.

    Both systems store the same thing; Specivo keys its redirects on slugs
    rather than titles, so the titles are slugified on the way in.
    """
    reader = getattr(ctx.adapter, "extract_wiki_redirects", None)
    if reader is None:
        return

    for project_ref in ctx.project_refs:
        project_id = await ctx.id_map.get(ctx.session, EntityType.PROJECT, project_ref)
        if project_id is None:
            continue
        wiki = await _wiki_service.get_wiki(ctx.session, project_id)
        if wiki is None:
            continue

        async for title, target in reader(project_ref):
            slug_from, slug_to = slugify(title), slugify(target)
            if not slug_from or not slug_to or slug_from == slug_to:
                continue

            existing = await ctx.session.execute(
                select(WikiRedirect.id)
                .where(WikiRedirect.wiki_id == wiki.id, WikiRedirect.title_from == slug_from)
                .limit(1)
            )
            if existing.scalar_one_or_none() is not None:
                continue

            ctx.session.add(WikiRedirect(wiki_id=wiki.id, title_from=slug_from, redirected_to=slug_to))
            ctx.tick()
    await ctx.session.flush()


async def _parent_slug(ctx: PhaseContext, parent_ref: str | None) -> str | None:
    """Return the slug of an imported parent page, if it was imported."""
    parent_id = await ctx.id_map.get(ctx.session, EntityType.WIKI_PAGE, parent_ref)
    if parent_id is None:
        return None
    parent = await ctx.session.get(WikiPage, parent_id)
    return parent.slug if parent else None


async def _resolve_author(ctx: PhaseContext, author_ref: str | None) -> User:
    """Return a revision's author, falling back to the import account."""
    author_id = await ctx.id_map.get(ctx.session, EntityType.USER, author_ref)
    if author_id is not None:
        author = await ctx.session.get(User, author_id)
        if author is not None:
            return author
    account = ctx.state.get(IMPORT_ACCOUNT_STATE_KEY)
    return account if account is not None else await ensure_import_account(ctx)
