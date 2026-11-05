"""Operating the link graph: full rebuilds and the figures shown in project settings.

A full rebuild re-reads every text source (issue descriptions, comments, live
wiki pages) of a project, or of the whole instance, and re-resolves what can
be resolved. It runs as one Celery task (``rebuild_link_graph_task``) and is
guarded twice, so only one runs per scope:

* the request takes a Redis lock (``TokenLock``, ``SET NX EX``) holding the
  task id before queueing, and answers 409 when it is held; the task refreshes
  it after each batch and releases it at the end (compare-and-delete), and a
  crashed worker's lock expires after ``LINK_REBUILD_LOCK_TTL``;
* the task takes ``pg_try_advisory_lock(LINK_REBUILD_LOCK_NAMESPACE, scope)``
  on a connection it keeps open, and does nothing when it is held: a
  redelivered or retried message, or an import rebuilding the same project
  (which takes the same key as a transaction lock).

Each source is rebuilt and committed on its own
(``EntityLinkService.rebuild_sources(..., commit_each=True)``), so the run
never holds one long transaction, and the queued per-source rebuilds that
saves trigger meanwhile interleave safely: both take the source's own lock
before reading its text, and whichever runs last reads the latest text.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import and_, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from specivo.models.attachment import Attachment
from specivo.models.entity_link import EntityLink
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.relation import IssueRelation
from specivo.models.user import User
from specivo.models.wiki import Wiki, WikiPage

logger = logging.getLogger(__name__)

#: First key of the advisory lock a full rebuild (and an import) takes; the second is the project id, 0 = instance.
LINK_REBUILD_LOCK_NAMESPACE = 0x5E50
#: Seconds the Redis run lock lives without a refresh; the task refreshes it after every batch.
LINK_REBUILD_LOCK_TTL = 300
#: Sources per progress report and lock refresh.
LINK_REBUILD_BATCH = 100
#: Unresolved targets listed in the stats.
STATS_TOP_UNRESOLVED = 5

_UNRESOLVED = and_(
    EntityLink.target_issue_id.is_(None),
    EntityLink.target_wiki_page_id.is_(None),
    EntityLink.target_attachment_id.is_(None),
)


def rebuild_lock_key(project_id: int | None) -> str:
    """Redis key of the run lock for a project, or the instance."""
    return "linkgraph:rebuild:all" if project_id is None else f"linkgraph:rebuild:{project_id}"


def last_result_key(project_id: int | None) -> str:
    """Settings key holding the summary of the last full rebuild."""
    base = "link_graph_rebuild_last_result"
    return base if project_id is None else f"{base}:project:{project_id}"


async def source_keys(session: AsyncSession, project_id: int | None) -> list[tuple[str, int]]:
    """Every text source of a project (or the instance): issues, comments with text, live wiki pages."""
    issues = select(Issue.id).order_by(Issue.id)
    journals = select(Journal.id).where(Journal.notes.is_not(None), Journal.notes != "").order_by(Journal.id)
    pages = (
        select(WikiPage.id)
        .join(Wiki, Wiki.id == WikiPage.wiki_id)
        .where(WikiPage.deleted_at.is_(None))
        .order_by(WikiPage.id)
    )
    if project_id is not None:
        issues = issues.where(Issue.project_id == project_id)
        journals = journals.where(Journal.project_id == project_id)
        pages = pages.where(Wiki.project_id == project_id)
    keys: list[tuple[str, int]] = []
    for source_type, stmt in (("issue", issues), ("journal", journals), ("wiki_page", pages)):
        keys.extend((source_type, source_id) for source_id in (await session.execute(stmt)).scalars())
    return keys


async def run_full_rebuild(
    factory: async_sessionmaker[AsyncSession],
    project_id: int | None,
    *,
    triggered_by: str | None = None,
    progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Rebuild every source of the scope, a source per transaction; return the summary it stores.

    *progress* is awaited after each batch with ``(processed, total)``; the
    task reports it and refreshes its run lock there. The caller holds the
    run's locks; this only does the work and writes the last result.
    """
    from specivo.core.utils import utcnow
    from specivo.services.entity_link_index import defer_entity_links
    from specivo.services.entity_link_service import EntityLinkService

    service = EntityLinkService()
    started = time.monotonic()
    started_at = utcnow().isoformat()
    async with factory() as session:
        keys = await source_keys(session, project_id)
    total = len(keys)
    failures: list[tuple[tuple[str, int], str]] = []
    for i in range(0, total, LINK_REBUILD_BATCH):
        batch = keys[i : i + LINK_REBUILD_BATCH]
        async with factory() as session:
            defer_entity_links(session)
            failures.extend(await service.rebuild_sources(session, batch, commit_each=True))
        if progress is not None:
            await progress(min(i + LINK_REBUILD_BATCH, total), total)

    async with factory() as session:
        stats = await edge_counts(session, project_id)
        summary: dict[str, Any] = {
            "status": "success" if not failures else "partial",
            "started_at": started_at,
            "finished_at": utcnow().isoformat(),
            "duration_seconds": round(time.monotonic() - started, 1),
            "triggered_by": triggered_by,
            "sources": total,
            "failed": len(failures),
            "errors": [f"{t} {i}: {reason}"[:300] for (t, i), reason in failures[:10]],
            "edges": stats["total"],
            "unresolved": stats["unresolved"],
        }
        await store_last_result(session, project_id, summary)
        await session.commit()
    return summary


async def store_last_result(session: AsyncSession, project_id: int | None, summary: dict[str, Any]) -> None:
    from specivo.services.settings_service import SettingsService

    await SettingsService().set_many(session, {last_result_key(project_id): json.dumps(summary)})


async def last_result(session: AsyncSession, project_id: int | None) -> dict[str, Any] | None:
    from specivo.services.settings_service import SettingsService

    raw = await SettingsService().get(session, last_result_key(project_id))
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


async def lock_for_import(session: AsyncSession, project_ids: list[int]) -> None:
    """Wait until no full rebuild runs on these projects, and keep them from starting until the transaction ends."""
    for project_id in sorted(set(project_ids)):
        await session.execute(select(func.pg_advisory_xact_lock(LINK_REBUILD_LOCK_NAMESPACE, project_id)))


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def _scope(project_id: int | None) -> Any:
    return true() if project_id is None else EntityLink.source_project_id == project_id


async def edge_counts(session: AsyncSession, project_id: int | None) -> dict[str, Any]:
    """Stored text links of the scope: total, unresolved, by kind, by source type, and the newest write.

    One ``GROUP BY`` over ``entity_links`` narrowed by the indexed
    ``source_project_id``.
    """
    rows = (
        await session.execute(
            select(
                EntityLink.source_type,
                EntityLink.kind,
                func.count(),
                func.count().filter(_UNRESOLVED),
                func.max(EntityLink.created_at),
            )
            .where(_scope(project_id))
            .group_by(EntityLink.source_type, EntityLink.kind)
        )
    ).all()
    by_kind: dict[str, int] = {}
    by_source: dict[str, int] = {"wiki_page": 0, "issue": 0, "journal": 0}
    total = unresolved = 0
    newest = None
    for source_type, kind, count, broken, latest in rows:
        by_kind[kind] = by_kind.get(kind, 0) + count
        by_source[source_type] = by_source.get(source_type, 0) + count
        total += count
        unresolved += broken
        if latest is not None and (newest is None or latest > newest):
            newest = latest
    return {
        "total": total,
        "unresolved": unresolved,
        "by_kind": by_kind,
        "by_source": by_source,
        "last_update": newest.isoformat() if newest is not None else None,
    }


async def structured_counts(session: AsyncSession, project_id: int | None) -> dict[str, int]:
    """Connections kept in their own tables, counted from the scope's side: relations, parents, attachments."""
    relations = select(func.count()).select_from(IssueRelation)
    parents = select(func.count()).select_from(Issue).where(Issue.parent_id.is_not(None))
    attachments = select(func.count()).select_from(Attachment)
    if project_id is not None:
        relations = relations.join(Issue, Issue.id == IssueRelation.issue_from_id).where(Issue.project_id == project_id)
        parents = parents.where(Issue.project_id == project_id)
        issue_ids = select(Issue.id).where(Issue.project_id == project_id)
        page_ids = select(WikiPage.id).join(Wiki, Wiki.id == WikiPage.wiki_id).where(Wiki.project_id == project_id)
        attachments = attachments.where(
            or_(
                and_(Attachment.container_type == "Issue", Attachment.container_id.in_(issue_ids)),
                and_(Attachment.container_type == "WikiPage", Attachment.container_id.in_(page_ids)),
            )
        )
    else:
        attachments = attachments.where(Attachment.container_type.in_(("Issue", "WikiPage")))
    return {
        "relation": int(await session.scalar(relations) or 0),
        "parent": int(await session.scalar(parents) or 0),
        "attachment": int(await session.scalar(attachments) or 0),
    }


async def top_unresolved(session: AsyncSession, user: User, project_id: int | None) -> list[dict[str, Any]]:
    """The most frequent unresolved targets, counted only over texts *user* may read.

    A target ref is raw text from its source, so a link written in an issue,
    comment or wiki page the viewer could not open is left out: an issue
    outside ``visible_issues_clause``, a private note for anyone but an
    admin, a wiki page without ``view_wiki``.
    """
    from specivo.models.project import Project
    from specivo.services.issue_service import IssueService
    from specivo.services.permission_service import Permission, projects_granting

    issue_visibility = await IssueService().visible_issues_clause(session, user)
    projects = (
        (await session.execute(select(Project).where(true() if project_id is None else Project.id == project_id)))
        .scalars()
        .all()
    )
    wiki_projects = await projects_granting(session, user, projects, Permission.VIEW_WIKI)

    journal = Journal.__table__.alias("link_journal")
    page = WikiPage.__table__.alias("link_page")
    page_wiki = Wiki.__table__.alias("link_page_wiki")
    issue_branch = and_(
        or_(EntityLink.source_issue_id.is_not(None), journal.c.issue_id.is_not(None)),
        issue_visibility if issue_visibility is not None else true(),
    )
    wiki_branch = and_(
        or_(EntityLink.source_wiki_page_id.is_not(None), journal.c.wiki_page_id.is_not(None)),
        page.c.deleted_at.is_(None),
        page_wiki.c.project_id.in_(wiki_projects),
    )
    stmt = (
        select(EntityLink.target_type, EntityLink.target_ref, EntityLink.kind, func.count().label("n"))
        .select_from(EntityLink)
        .outerjoin(journal, journal.c.id == EntityLink.source_journal_id)
        .outerjoin(Issue, Issue.id == func.coalesce(EntityLink.source_issue_id, journal.c.issue_id))
        .outerjoin(page, page.c.id == func.coalesce(EntityLink.source_wiki_page_id, journal.c.wiki_page_id))
        .outerjoin(page_wiki, page_wiki.c.id == page.c.wiki_id)
        .where(
            _scope(project_id),
            _UNRESOLVED,
            or_(issue_branch, wiki_branch),
            or_(journal.c.id.is_(None), journal.c.is_private.is_(False), true() if user.is_admin else false()),
        )
        .group_by(EntityLink.target_type, EntityLink.target_ref, EntityLink.kind)
        .order_by(func.count().desc(), EntityLink.target_ref)
        .limit(STATS_TOP_UNRESOLVED)
    )
    return [
        {"target_type": target_type, "target_ref": ref, "kind": kind, "count": count}
        for target_type, ref, kind, count in await session.execute(stmt)
    ]


async def project_stats(session: AsyncSession, user: User, project_id: int | None) -> dict[str, Any]:
    """The figures of the "Link graph" card: stored text links, structured links, unresolved targets, last rebuild.

    "Sources indexed vs total" is not reported: a source without links stores
    no row, so an indexed source cannot be told from a missed one.
    """
    edges = await edge_counts(session, project_id)
    return {
        **edges,
        "structured": await structured_counts(session, project_id),
        "top_unresolved": await top_unresolved(session, user, project_id),
        "last_rebuild": await last_result(session, project_id),
    }
