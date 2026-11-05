"""EntityLinkService: the stored graph of links written in text, and reads that merge it with structured links.

Writing
-------
:meth:`EntityLinkService.rebuild_source` replaces every ``entity_links`` row of
one source text (an issue description, a comment, or a wiki page's latest
content) with the links :func:`markdown_service.extract_links` finds in it now.
It is idempotent: rebuilding a source twice gives the same rows. A target that
does not exist yet is stored unresolved (``target_*_id`` NULL, ``target_ref``
kept) and :meth:`EntityLinkService.resolve_incoming` fills it in once the issue
or page is created, renamed or restored.

Both take a transaction-level advisory lock per target project, so a link
parsed while its target is being created is never left unresolved: either the
parse sees the committed target, or the resolver sees the committed link.

Reading
-------
:meth:`EntityLinkService.get_links` returns the neighbours of an issue or a
wiki page: text links from ``entity_links`` merged with relations, parent and
child, attachments, commits and pull requests read live from their own tables.
Everything is filtered for the reader. A neighbour the reader may not see is
left out together with its edge (never shown as "restricted"), as the Markdown
renderer leaves an issue reference the reader may not see unlinked.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import (
    ENTITY_LINKS_DEFAULT_LIMIT,
    ENTITY_LINKS_MAX_EXPANDED,
    ENTITY_LINKS_MAX_LIMIT,
    ENTITY_LINKS_MAX_PER_SOURCE,
)
from specivo.models.attachment import Attachment
from specivo.models.entity_link import (
    KIND_ATTACHMENT_LINK,
    KIND_MENTION,
    KIND_WIKI_LINK,
    SOURCE_ISSUE,
    SOURCE_JOURNAL,
    SOURCE_WIKI_PAGE,
    TARGET_ATTACHMENT,
    TARGET_ISSUE,
    TARGET_WIKI_PAGE,
    EntityLink,
)
from specivo.models.issue import Issue, IssueRefAlias
from specivo.models.journal import Journal
from specivo.models.lookups import IssueStatus
from specivo.models.project import EnabledModule, Project, ProjectKeyAlias
from specivo.models.relation import IssueRelation
from specivo.models.user import User
from specivo.models.wiki import Wiki, WikiContent, WikiPage, WikiRedirect
from specivo.services.api_key_scope import admin_reaches, is_global_admin
from specivo.services.markdown_service import FoundLink, extract_links

logger = logging.getLogger(__name__)

SourceKey = tuple[str, int]

# First key of the two-key advisory lock taken per target project.
_LOCK_NAMESPACE = 0x5E4C
# Values per ``IN (...)`` when resolving targets.
_RESOLVE_BATCH = 100

# Read-side kinds. Text kinds are stored; structured kinds are read live.
KIND_RELATION = "relation"
KIND_PARENT = "parent"
KIND_CHILD = "child"
KIND_ATTACHMENT = "attachment"
KIND_COMMIT = "commit"
KIND_PULL_REQUEST = "pull_request"
TEXT_LINK_KINDS = frozenset({KIND_WIKI_LINK, KIND_MENTION, KIND_ATTACHMENT_LINK})
STRUCTURED_KINDS = frozenset({KIND_RELATION, KIND_PARENT, KIND_CHILD, KIND_ATTACHMENT, KIND_COMMIT, KIND_PULL_REQUEST})
ALL_KINDS = TEXT_LINK_KINDS | STRUCTURED_KINDS

DIRECTIONS = ("in", "out", "both")

NodeKey = tuple[str, int]  # ("issue" | "wiki_page" | "attachment" | "commit" | "pull_request", id)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Source:
    source_type: str
    source_id: int
    project_id: int
    text: str
    # The entity whose text this is: an issue or a wiki page (a comment's parent).
    owner: NodeKey
    # Files a bare filename may name, as the renderer resolves them for this text.
    file_scope: str  # "issue" (the issue and its comments) or "project" (wiki pages)


@dataclass(slots=True)
class _Row:
    kind: str
    target_type: str
    target_project_id: int | None
    target_ref: str
    display_text: str | None
    target_issue_id: int | None = None
    target_wiki_page_id: int | None = None
    target_attachment_id: int | None = None

    @property
    def key(self) -> tuple[str, str, int | None, str]:
        return (self.kind, self.target_type, self.target_project_id, self.target_ref)


def _clip(text: str | None, size: int = 255) -> str | None:
    return text[:size] if text else None


async def _lock_projects(session: AsyncSession, project_ids: Iterable[int | None]) -> None:
    """Take the link lock of each project, in id order so two writers never deadlock."""
    for project_id in sorted({p for p in project_ids if p is not None}):
        await session.execute(select(func.pg_advisory_xact_lock(_LOCK_NAMESPACE, project_id)))


# First keys of the two-key advisory lock taken per source, one per source type.
_SOURCE_LOCK_NAMESPACES = {SOURCE_ISSUE: 0x5E4D, SOURCE_JOURNAL: 0x5E4E, SOURCE_WIKI_PAGE: 0x5E4F}


async def _lock_source(session: AsyncSession, source_type: str, source_id: int) -> None:
    namespace = _SOURCE_LOCK_NAMESPACES.get(source_type)
    if namespace is None:
        raise ValueError(f"Unknown link source type: {source_type!r}")
    await session.execute(select(func.pg_advisory_xact_lock(namespace, source_id)))


def _instance_origin() -> str | None:
    from specivo.core.config import get_settings

    try:
        return get_settings().app_url
    except Exception:  # noqa: BLE001 - outside a configured environment no URL is this instance's
        return None


class EntityLinkService:
    """Build and read the entity link graph."""

    # -- writing ------------------------------------------------------------

    async def rebuild_source(self, session: AsyncSession, source_type: str, source_id: int) -> int:
        """Replace the stored links of one source with the links its text holds now.

        A source that no longer exists, or a wiki page in the trash, keeps no
        links. When the source is an issue or a wiki page, links that other
        texts wrote to it before it existed (or under its new slug) are
        resolved too. Returns the number of links stored.

        The source's own lock is taken before its text is read, so two
        rebuilds of one source (a queued one after a save, and a full rebuild
        of the project) run one after the other, and the later one reads the
        text the earlier one could not see yet. Call it in a transaction of
        its own (``rebuild_sources(..., commit_each=True)``) wherever other
        writers may run, so the locks are held only for one source.
        """
        await _lock_source(session, source_type, source_id)
        source = await self._load_source(session, source_type, source_id)
        if source is None:
            await self._delete_source_links(session, source_type, source_id)
            await self._resolve_self(session, source_type, source_id)
            return 0

        rows = await self._resolve(session, source, extract_links(source.text, _instance_origin()))
        await _lock_projects(session, [source.project_id, *(r.target_project_id for r in rows)])
        await self._delete_source_links(session, source_type, source_id)
        if rows:
            column = {
                SOURCE_ISSUE: "source_issue_id",
                SOURCE_JOURNAL: "source_journal_id",
                SOURCE_WIKI_PAGE: "source_wiki_page_id",
            }[source_type]
            session.add_all(
                EntityLink(
                    source_type=source_type,
                    source_project_id=source.project_id,
                    kind=r.kind,
                    target_type=r.target_type,
                    target_issue_id=r.target_issue_id,
                    target_wiki_page_id=r.target_wiki_page_id,
                    target_attachment_id=r.target_attachment_id,
                    target_project_id=r.target_project_id,
                    target_ref=r.target_ref,
                    display_text=r.display_text,
                    **{column: source_id},
                )
                for r in rows
            )
        await session.flush()
        await self._resolve_self(session, source_type, source_id)
        return len(rows)

    async def expand_keys(self, session: AsyncSession, keys: Iterable[SourceKey]) -> list[SourceKey]:
        """Turn the keys ``entity_link_index`` queues into link sources, in a stable order.

        ``("issue_comments", issue_id)`` stands for every comment of the
        issue, and ``("attachment", id)`` for the sources holding an
        unresolved link that names the new file.
        """
        from specivo.services.entity_link_index import ATTACHMENT, ISSUE_COMMENTS

        sources: set[SourceKey] = set()
        for key_type, key_id in keys:
            if key_type in (SOURCE_ISSUE, SOURCE_JOURNAL, SOURCE_WIKI_PAGE):
                sources.add((key_type, key_id))
            elif key_type == ISSUE_COMMENTS:
                rows = await session.execute(
                    select(Journal.id).where(Journal.issue_id == key_id, Journal.notes.is_not(None))
                )
                sources.update((SOURCE_JOURNAL, journal_id) for journal_id in rows.scalars())
            elif key_type == ATTACHMENT:
                sources.update(await self._sources_waiting_for_file(session, key_id))
            else:
                logger.warning("Ignoring unknown link source type %r", key_type)
        return sorted(sources)

    async def _sources_waiting_for_file(self, session: AsyncSession, attachment_id: int) -> set[SourceKey]:
        attachment = (
            await session.execute(
                select(Attachment.filename, Attachment.container_type, Attachment.container_id).where(
                    Attachment.id == attachment_id
                )
            )
        ).one_or_none()
        if attachment is None:
            return set()
        if attachment.container_type == "Issue":
            project_id = await session.scalar(select(Issue.project_id).where(Issue.id == attachment.container_id))
        elif attachment.container_type == "WikiPage":
            project_id = await session.scalar(
                select(Wiki.project_id)
                .join(WikiPage, WikiPage.wiki_id == Wiki.id)
                .where(WikiPage.id == attachment.container_id)
            )
        elif attachment.container_type == "Journal":
            project_id = await session.scalar(
                select(Issue.project_id)
                .join(Journal, Journal.issue_id == Issue.id)
                .where(Journal.id == attachment.container_id)
            )
        else:
            project_id = None
        if project_id is None:
            return set()
        rows = await session.execute(
            select(EntityLink.source_issue_id, EntityLink.source_journal_id, EntityLink.source_wiki_page_id).where(
                EntityLink.kind == KIND_ATTACHMENT_LINK,
                EntityLink.target_project_id == project_id,
                EntityLink.target_ref == attachment.filename,
                EntityLink.target_attachment_id.is_(None),
            )
        )
        found: set[SourceKey] = set()
        for issue_id, journal_id, page_id in rows:
            if issue_id is not None:
                found.add((SOURCE_ISSUE, issue_id))
            elif journal_id is not None:
                found.add((SOURCE_JOURNAL, journal_id))
            elif page_id is not None:
                found.add((SOURCE_WIKI_PAGE, page_id))
        return found

    async def rebuild_sources(
        self, session: AsyncSession, keys: Iterable[SourceKey], *, commit_each: bool = False
    ) -> list[tuple[SourceKey, str]]:
        """Rebuild several sources; return the ones that failed with the reason.

        Each source is rebuilt in a savepoint, so one that fails leaves the
        others alone. With ``commit_each`` every source is also committed on
        its own, which releases its locks at once: the queued task, the full
        rebuild and the CLI do this, so they never hold one source's lock
        while waiting for another's. The import pipeline cannot (its phase is
        one transaction) and runs while no queued rebuild touches its rows.
        """
        failures: list[tuple[SourceKey, str]] = []
        for source_type, source_id in keys:
            # An explicit savepoint rather than ``async with``: ending it must
            # leave the session free to begin another one straight away.
            savepoint = await session.begin_nested()
            try:
                await self.rebuild_source(session, source_type, source_id)
            except Exception as exc:  # noqa: BLE001 - one bad text must not stop the others
                if savepoint.is_active:
                    await savepoint.rollback()
                logger.warning("Could not rebuild links of %s %d: %s", source_type, source_id, exc)
                failures.append(((source_type, source_id), str(exc)))
            else:
                await savepoint.commit()
            if commit_each:
                await session.commit()
        return failures

    async def resolve_incoming(self, session: AsyncSession, target_type: str, target_id: int) -> int:
        """Point unresolved links that name this issue or wiki page at it; return how many."""
        if target_type == TARGET_ISSUE:
            row = (
                await session.execute(select(Issue.project_id, Issue.sequence_number).where(Issue.id == target_id))
            ).one_or_none()
            if row is None:
                return 0
            project_id, number = row
            await _lock_projects(session, [project_id])
            # The key part of target_ref may be a previous key of the project.
            stmt = (
                update(EntityLink)
                .where(
                    EntityLink.target_type == TARGET_ISSUE,
                    EntityLink.target_project_id == project_id,
                    func.split_part(EntityLink.target_ref, "-", 2) == str(number),
                    EntityLink.target_issue_id.is_(None),
                )
                .values(target_issue_id=target_id)
            )
        elif target_type == TARGET_WIKI_PAGE:
            page = (
                await session.execute(
                    select(Wiki.project_id, WikiPage.slug, WikiPage.deleted_at)
                    .join(Wiki, Wiki.id == WikiPage.wiki_id)
                    .where(WikiPage.id == target_id)
                )
            ).one_or_none()
            if page is None or page.deleted_at is not None:
                return 0
            await _lock_projects(session, [page.project_id])
            stmt = (
                update(EntityLink)
                .where(
                    EntityLink.target_type == TARGET_WIKI_PAGE,
                    EntityLink.target_project_id == page.project_id,
                    EntityLink.target_ref == page.slug,
                    EntityLink.target_wiki_page_id.is_(None),
                )
                .values(target_wiki_page_id=target_id)
            )
        else:
            return 0
        result = await session.execute(stmt.execution_options(synchronize_session=False))
        count = int(getattr(result, "rowcount", 0) or 0)
        if count:
            logger.debug("Resolved %d incoming link(s) to %s %d", count, target_type, target_id)
        return count

    async def _resolve_self(self, session: AsyncSession, source_type: str, source_id: int) -> None:
        if source_type == SOURCE_ISSUE:
            await self.resolve_incoming(session, TARGET_ISSUE, source_id)
        elif source_type == SOURCE_WIKI_PAGE:
            await self.resolve_incoming(session, TARGET_WIKI_PAGE, source_id)

    async def _delete_source_links(self, session: AsyncSession, source_type: str, source_id: int) -> None:
        column = {
            SOURCE_ISSUE: EntityLink.source_issue_id,
            SOURCE_JOURNAL: EntityLink.source_journal_id,
            SOURCE_WIKI_PAGE: EntityLink.source_wiki_page_id,
        }.get(source_type)
        if column is None:
            raise ValueError(f"Unknown link source type: {source_type!r}")
        await session.execute(
            delete(EntityLink).where(column == source_id).execution_options(synchronize_session=False)
        )

    async def _load_source(self, session: AsyncSession, source_type: str, source_id: int) -> _Source | None:
        if source_type == SOURCE_ISSUE:
            row = (
                await session.execute(select(Issue.project_id, Issue.description).where(Issue.id == source_id))
            ).one_or_none()
            if row is None:
                return None
            return _Source(source_type, source_id, row.project_id, row.description or "", ("issue", source_id), "issue")
        if source_type == SOURCE_JOURNAL:
            journal = (
                await session.execute(
                    select(Journal.project_id, Journal.notes, Journal.issue_id, Journal.wiki_page_id).where(
                        Journal.id == source_id
                    )
                )
            ).one_or_none()
            if journal is None:
                return None
            if journal.issue_id is not None:
                owner: NodeKey = ("issue", journal.issue_id)
                scope = "issue"
                project_id = await session.scalar(select(Issue.project_id).where(Issue.id == journal.issue_id))
            else:
                owner = ("wiki_page", journal.wiki_page_id)
                scope = "project"
                project_id = await session.scalar(
                    select(Wiki.project_id)
                    .join(WikiPage, WikiPage.wiki_id == Wiki.id)
                    .where(WikiPage.id == journal.wiki_page_id)
                )
            if project_id is None:
                return None
            return _Source(source_type, source_id, project_id, journal.notes or "", owner, scope)
        if source_type == SOURCE_WIKI_PAGE:
            page = (
                await session.execute(
                    select(WikiPage.deleted_at, Wiki.project_id)
                    .join(Wiki, Wiki.id == WikiPage.wiki_id)
                    .where(WikiPage.id == source_id)
                )
            ).one_or_none()
            if page is None or page.deleted_at is not None:
                return None
            text = await session.scalar(
                select(WikiContent.text)
                .where(WikiContent.page_id == source_id)
                .order_by(WikiContent.version.desc())
                .limit(1)
            )
            return _Source(source_type, source_id, page.project_id, text or "", ("wiki_page", source_id), "project")
        raise ValueError(f"Unknown link source type: {source_type!r}")

    async def _resolve(self, session: AsyncSession, source: _Source, found: Sequence[FoundLink]) -> list[_Row]:
        """Turn what the parse found into rows, resolving each target that exists now."""
        rows: dict[tuple[str, str, int | None, str], _Row] = {}
        project_ids = await self._project_ids_by_key(
            session,
            {link.project_key for link in found if link.project_key}
            | {link.target.rpartition("-")[0] for link in found if link.kind == "issue"},
        )

        def add(row: _Row) -> None:
            if row.key not in rows and len(rows) < ENTITY_LINKS_MAX_PER_SOURCE:
                rows[row.key] = row
            elif len(rows) >= ENTITY_LINKS_MAX_PER_SOURCE and row.key not in rows:
                raise _CapReachedError

        try:
            for link in found:
                if link.kind == "wiki":
                    if link.project_key is None:
                        project_id: int | None = source.project_id
                        kind = KIND_WIKI_LINK
                    else:
                        project_id = project_ids.get(link.project_key)
                        kind = KIND_MENTION
                    if project_id is None or not link.target:
                        continue
                    add(_Row(kind, TARGET_WIKI_PAGE, project_id, link.target[:255], _clip(link.label)))
                elif link.kind == "issue":
                    key = link.target.rpartition("-")[0]
                    project_id = project_ids.get(key)
                    if project_id is None:
                        # Not a project key (``UTF-8``, ``SHA-256``): not an issue reference.
                        continue
                    add(_Row(KIND_MENTION, TARGET_ISSUE, project_id, link.target, _clip(link.label)))
                elif link.kind == "file":
                    add(
                        _Row(
                            KIND_ATTACHMENT_LINK,
                            TARGET_ATTACHMENT,
                            source.project_id,
                            link.target[:255],
                            _clip(link.label),
                        )
                    )
                elif link.kind == "attachment":
                    add(_Row(KIND_MENTION, TARGET_ATTACHMENT, None, link.target, _clip(link.label)))
        except _CapReachedError:
            logger.warning(
                "Link cap reached (%d): stopped storing links of %s %d",
                ENTITY_LINKS_MAX_PER_SOURCE,
                source.source_type,
                source.source_id,
            )

        result = list(rows.values())
        await self._resolve_wiki_targets(session, [r for r in result if r.target_type == TARGET_WIKI_PAGE])
        await self._resolve_issue_targets(session, [r for r in result if r.target_type == TARGET_ISSUE])
        await self._resolve_attachment_targets(
            session, source, [r for r in result if r.target_type == TARGET_ATTACHMENT]
        )
        # A text naming its own issue or page is not a link to anything else.
        kind, owner_id = source.owner
        return [
            r
            for r in result
            if not (
                (kind == "issue" and r.target_issue_id == owner_id)
                or (kind == "wiki_page" and r.target_wiki_page_id == owner_id)
            )
        ]

    async def _project_ids_by_key(self, session: AsyncSession, keys: set[str]) -> dict[str, int]:
        keys = {k.upper() for k in keys if k}
        if not keys:
            return {}
        found: dict[str, int] = {}
        for key, project_id in await session.execute(select(Project.key, Project.id).where(Project.key.in_(keys))):
            found[key] = project_id
        missing = keys - found.keys()
        if missing:
            for key, project_id in await session.execute(
                select(ProjectKeyAlias.old_key, ProjectKeyAlias.project_id).where(ProjectKeyAlias.old_key.in_(missing))
            ):
                found[key] = project_id
        return found

    async def _resolve_wiki_targets(self, session: AsyncSession, rows: list[_Row]) -> None:
        by_project: dict[int, list[_Row]] = defaultdict(list)
        for row in rows:
            if row.target_project_id is not None:
                by_project[row.target_project_id].append(row)
        for project_id, project_rows in by_project.items():
            wiki_id = await session.scalar(select(Wiki.id).where(Wiki.project_id == project_id))
            if wiki_id is None:
                continue
            slugs = sorted({r.target_ref for r in project_rows})
            pages: dict[str, int] = {}
            for i in range(0, len(slugs), _RESOLVE_BATCH):
                batch = slugs[i : i + _RESOLVE_BATCH]
                for page_id, slug in await session.execute(
                    select(WikiPage.id, WikiPage.slug).where(
                        WikiPage.wiki_id == wiki_id, WikiPage.slug.in_(batch), WikiPage.deleted_at.is_(None)
                    )
                ):
                    pages[slug] = page_id
                # A renamed page is still reached under its old slug, as on the page.
                missing = [s for s in batch if s not in pages]
                if missing:
                    for old, page_id in await session.execute(
                        select(WikiRedirect.title_from, WikiPage.id)
                        .join(
                            WikiPage,
                            and_(WikiPage.wiki_id == WikiRedirect.wiki_id, WikiPage.slug == WikiRedirect.redirected_to),
                        )
                        .where(
                            WikiRedirect.wiki_id == wiki_id,
                            WikiRedirect.title_from.in_(missing),
                            WikiPage.deleted_at.is_(None),
                        )
                    ):
                        pages[old] = page_id
            for row in project_rows:
                row.target_wiki_page_id = pages.get(row.target_ref)

    async def _resolve_issue_targets(self, session: AsyncSession, rows: list[_Row]) -> None:
        refs = sorted({r.target_ref for r in rows})
        pairs: list[tuple[str, int]] = []
        for ref in refs:
            ref_key, _, ref_number = ref.rpartition("-")
            pairs.append((ref_key, int(ref_number)))
        ids: dict[str, int] = {}
        for i in range(0, len(pairs), _RESOLVE_BATCH):
            batch = pairs[i : i + _RESOLVE_BATCH]
            for key, number, issue_id in await session.execute(
                select(Issue.project_key, Issue.sequence_number, Issue.id).where(
                    tuple_(Issue.project_key, Issue.sequence_number).in_(batch)
                )
            ):
                ids[f"{key}-{number}"] = issue_id
            rest = [p for p in batch if f"{p[0]}-{p[1]}" not in ids]
            if rest:
                for key, number, issue_id in await session.execute(
                    select(
                        IssueRefAlias.old_project_key, IssueRefAlias.old_sequence_number, IssueRefAlias.issue_id
                    ).where(tuple_(IssueRefAlias.old_project_key, IssueRefAlias.old_sequence_number).in_(rest))
                ):
                    ids[f"{key}-{number}"] = issue_id
            rest = [p for p in rest if f"{p[0]}-{p[1]}" not in ids]
            if rest:
                for key, number, issue_id in await session.execute(
                    select(ProjectKeyAlias.old_key, Issue.sequence_number, Issue.id)
                    .join(Issue, Issue.project_id == ProjectKeyAlias.project_id)
                    .where(tuple_(ProjectKeyAlias.old_key, Issue.sequence_number).in_(rest))
                ):
                    ids[f"{key}-{number}"] = issue_id
        for row in rows:
            row.target_issue_id = ids.get(row.target_ref)

    async def _resolve_attachment_targets(self, session: AsyncSession, source: _Source, rows: list[_Row]) -> None:
        by_id = [r for r in rows if r.kind == KIND_MENTION]
        if by_id:
            wanted = {int(r.target_ref) for r in by_id}
            existing = set(
                (await session.execute(select(Attachment.id).where(Attachment.id.in_(wanted)))).scalars().all()
            )
            for row in by_id:
                if int(row.target_ref) in existing:
                    row.target_attachment_id = int(row.target_ref)

        by_name = [r for r in rows if r.kind == KIND_ATTACHMENT_LINK]
        if not by_name:
            return
        names = sorted({r.target_ref for r in by_name})
        if source.file_scope == "issue":
            _, issue_id = source.owner
            journal_ids = select(Journal.id).where(Journal.issue_id == issue_id)
            scope = or_(
                and_(Attachment.container_type == "Issue", Attachment.container_id == issue_id),
                and_(Attachment.container_type == "Journal", Attachment.container_id.in_(journal_ids)),
            )
        else:
            # Wiki pages resolve a filename among the project's issue and wiki-page files.
            issue_ids = select(Issue.id).where(Issue.project_id == source.project_id)
            page_ids = (
                select(WikiPage.id).join(Wiki, Wiki.id == WikiPage.wiki_id).where(Wiki.project_id == source.project_id)
            )
            scope = or_(
                and_(Attachment.container_type == "Issue", Attachment.container_id.in_(issue_ids)),
                and_(Attachment.container_type == "WikiPage", Attachment.container_id.in_(page_ids)),
            )
        files: dict[str, int] = {}
        for attachment_id, filename in await session.execute(
            select(Attachment.id, Attachment.filename)
            .where(scope, Attachment.filename.in_(names))
            .order_by(Attachment.created_at.asc(), Attachment.id.asc())
        ):
            files[filename] = attachment_id  # the newest wins, as when rendering
        for row in by_name:
            row.target_attachment_id = files.get(row.target_ref)

    # -- reading ------------------------------------------------------------

    async def get_links(
        self,
        session: AsyncSession,
        user: User,
        root: NodeKey,
        *,
        direction: str = "both",
        depth: int = 1,
        kinds: Iterable[str] | None = None,
        limit: int = ENTITY_LINKS_DEFAULT_LIMIT,
    ) -> LinkGraph:
        """Return the neighbours of *root* (an issue or a wiki page) that *user* may see.

        The caller has already checked that *user* may see *root*.

        - ``direction``: ``out`` (what *root* links to, plus its relations,
          parent, children, attachments, commits and pull requests), ``in``
          (text links to *root*: backlinks) or ``both``.
        - ``depth``: 1, or 2 to expand the issue and wiki-page neighbours once
          more (at most ``ENTITY_LINKS_MAX_EXPANDED`` of them).
        - ``kinds``: only these edge kinds (``ALL_KINDS``); all when None.
        - ``limit``: at most this many neighbour nodes besides *root*
          (capped at ``ENTITY_LINKS_MAX_LIMIT``); ``truncated`` says when
          more were left out.
        """
        if direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of {', '.join(DIRECTIONS)}")
        if depth not in (1, 2):
            raise ValueError("depth must be 1 or 2")
        wanted = ALL_KINDS if kinds is None else frozenset(kinds)
        unknown = wanted - ALL_KINDS
        if unknown:
            raise ValueError(f"Unknown link kind(s): {', '.join(sorted(unknown))}")
        limit = max(1, min(limit, ENTITY_LINKS_MAX_LIMIT))

        reader = _Reader(session, user)
        graph = LinkGraph()
        seen_nodes: set[NodeKey] = {root}
        order: list[NodeKey] = [root]
        frontier: list[NodeKey] = [root]
        for hop in range(depth):
            raw: list[_RawEdge] = []
            for node in frontier:
                raw.extend(await self._raw_edges(session, node, direction, wanted))
            edges = await reader.filter(raw)
            next_frontier: list[NodeKey] = []
            for edge in edges:
                for endpoint in (edge.source, edge.target):
                    if endpoint is None or endpoint in seen_nodes:
                        continue
                    if len(seen_nodes) - 1 >= limit:
                        graph.truncated = True
                        break
                    seen_nodes.add(endpoint)
                    order.append(endpoint)
                    if endpoint[0] in ("issue", "wiki_page"):
                        next_frontier.append(endpoint)
                if (edge.source is None or edge.source in seen_nodes) and (
                    edge.target is None or edge.target in seen_nodes
                ):
                    graph.raw_edges.append((edge, hop == 0 and root in (edge.source, edge.target)))
            if len(next_frontier) > ENTITY_LINKS_MAX_EXPANDED:
                next_frontier = next_frontier[:ENTITY_LINKS_MAX_EXPANDED]
                graph.truncated = True
            frontier = next_frontier
        await reader.describe(graph, root, order)
        return graph

    async def _raw_edges(
        self, session: AsyncSession, node: NodeKey, direction: str, kinds: frozenset[str]
    ) -> list[_RawEdge]:
        node_type, node_id = node
        edges: list[_RawEdge] = []
        text_kinds = kinds & TEXT_LINK_KINDS
        if text_kinds and direction in ("out", "both"):
            edges.extend(await self._text_edges_out(session, node, text_kinds))
        if text_kinds and direction in ("in", "both"):
            edges.extend(await self._text_edges_in(session, node, text_kinds))
        if direction == "in":
            return edges

        if node_type == "issue":
            if KIND_RELATION in kinds:
                from specivo.services.relation_service import RELATION_TYPES

                for rel in (
                    await session.execute(
                        select(IssueRelation).where(
                            or_(IssueRelation.issue_from_id == node_id, IssueRelation.issue_to_id == node_id)
                        )
                    )
                ).scalars():
                    label: str | None
                    if rel.issue_from_id == node_id:
                        other, label = rel.issue_to_id, rel.relation_type
                    else:
                        other, label = rel.issue_from_id, RELATION_TYPES[rel.relation_type]["sym"]
                    edges.append(_RawEdge(node, ("issue", other), KIND_RELATION, relation_type=label))
            if KIND_PARENT in kinds:
                parent_id = await session.scalar(select(Issue.parent_id).where(Issue.id == node_id))
                if parent_id is not None:
                    edges.append(_RawEdge(node, ("issue", parent_id), KIND_PARENT))
            if KIND_CHILD in kinds:
                for child_id in (await session.execute(select(Issue.id).where(Issue.parent_id == node_id))).scalars():
                    edges.append(_RawEdge(node, ("issue", child_id), KIND_CHILD))
            if KIND_ATTACHMENT in kinds:
                for attachment_id in (
                    await session.execute(
                        select(Attachment.id).where(
                            Attachment.container_type == "Issue", Attachment.container_id == node_id
                        )
                    )
                ).scalars():
                    edges.append(_RawEdge(node, ("attachment", attachment_id), KIND_ATTACHMENT))
            if KIND_COMMIT in kinds or KIND_PULL_REQUEST in kinds:
                from specivo.models.repository import IssueCommitLink, IssuePullRequestLink

                if KIND_COMMIT in kinds:
                    for commit_id in (
                        await session.execute(
                            select(IssueCommitLink.commit_id).where(IssueCommitLink.issue_id == node_id)
                        )
                    ).scalars():
                        edges.append(_RawEdge(node, ("commit", commit_id), KIND_COMMIT))
                if KIND_PULL_REQUEST in kinds:
                    for pr_id in (
                        await session.execute(
                            select(IssuePullRequestLink.pull_request_id).where(IssuePullRequestLink.issue_id == node_id)
                        )
                    ).scalars():
                        edges.append(_RawEdge(node, ("pull_request", pr_id), KIND_PULL_REQUEST))
        elif node_type == "wiki_page":
            if KIND_PARENT in kinds:
                parent_id = await session.scalar(select(WikiPage.parent_id).where(WikiPage.id == node_id))
                if parent_id is not None:
                    edges.append(_RawEdge(node, ("wiki_page", parent_id), KIND_PARENT))
            if KIND_CHILD in kinds:
                for child_id in (
                    await session.execute(select(WikiPage.id).where(WikiPage.parent_id == node_id))
                ).scalars():
                    edges.append(_RawEdge(node, ("wiki_page", child_id), KIND_CHILD))
            if KIND_ATTACHMENT in kinds:
                for attachment_id in (
                    await session.execute(
                        select(Attachment.id).where(
                            Attachment.container_type == "WikiPage", Attachment.container_id == node_id
                        )
                    )
                ).scalars():
                    edges.append(_RawEdge(node, ("attachment", attachment_id), KIND_ATTACHMENT))
        return edges

    async def _text_edges_out(self, session: AsyncSession, node: NodeKey, kinds: frozenset[str]) -> list[_RawEdge]:
        node_type, node_id = node
        if node_type == "issue":
            own = EntityLink.source_issue_id == node_id
            journals = select(Journal.id).where(Journal.issue_id == node_id)
        elif node_type == "wiki_page":
            own = EntityLink.source_wiki_page_id == node_id
            journals = select(Journal.id).where(Journal.wiki_page_id == node_id)
        else:
            return []
        rows = await session.execute(
            select(EntityLink).where(or_(own, EntityLink.source_journal_id.in_(journals)), EntityLink.kind.in_(kinds))
        )
        edges = []
        for link in rows.scalars():
            target = _target_node(link)
            edges.append(
                _RawEdge(
                    node,
                    target,
                    link.kind,
                    label=link.display_text,
                    via_journal_id=link.source_journal_id,
                    broken_ref=None if target is not None else _broken_ref(link),
                    broken_project_id=link.target_project_id,
                    broken_target_type=link.target_type,
                )
            )
        return edges

    async def _text_edges_in(self, session: AsyncSession, node: NodeKey, kinds: frozenset[str]) -> list[_RawEdge]:
        node_type, node_id = node
        column = {"issue": EntityLink.target_issue_id, "wiki_page": EntityLink.target_wiki_page_id}.get(node_type)
        if column is None:
            return []
        rows = (
            await session.execute(
                select(EntityLink, Journal.issue_id, Journal.wiki_page_id)
                .outerjoin(Journal, Journal.id == EntityLink.source_journal_id)
                .where(column == node_id, EntityLink.kind.in_(kinds))
            )
        ).all()
        edges = []
        for link, journal_issue_id, journal_page_id in rows:
            if link.source_issue_id is not None:
                source: NodeKey = ("issue", link.source_issue_id)
            elif link.source_wiki_page_id is not None:
                source = ("wiki_page", link.source_wiki_page_id)
            elif journal_issue_id is not None:
                source = ("issue", journal_issue_id)
            elif journal_page_id is not None:
                source = ("wiki_page", journal_page_id)
            else:
                continue
            if source == node:
                continue
            edges.append(
                _RawEdge(source, node, link.kind, label=link.display_text, via_journal_id=link.source_journal_id)
            )
        return edges

    # -- wiki graph (GET /projects/{key}/wiki/graph/) -------------------------

    async def get_wiki_graph(self, session: AsyncSession, wiki_id: int) -> dict[str, list[dict[str, Any]]]:
        """Return ``[[...]]`` links between the live pages of a wiki as ``{"nodes": [...], "edges": [...]}``.

        A link to a page that does not exist, or is in the trash, is broken.
        """
        pages = (
            await session.execute(
                select(WikiPage.id, WikiPage.slug, WikiPage.title).where(
                    WikiPage.wiki_id == wiki_id, WikiPage.deleted_at.is_(None)
                )
            )
        ).all()
        nodes = [{"id": r.id, "slug": r.slug, "title": r.title} for r in pages]
        live = {r.id for r in pages}
        rows = await session.execute(
            select(
                EntityLink.source_wiki_page_id,
                EntityLink.target_wiki_page_id,
                EntityLink.target_ref,
                EntityLink.display_text,
            ).where(EntityLink.source_wiki_page_id.in_(live), EntityLink.kind == KIND_WIKI_LINK)
        )
        edges = []
        for source_id, target_id, slug, label in rows:
            target = target_id if target_id in live else None
            edges.append(
                {
                    "source_page_id": source_id,
                    "target_page_id": target,
                    "target_slug": slug,
                    "display_text": label,
                    "is_broken": target is None,
                }
            )
        return {"nodes": nodes, "edges": edges}


class _CapReachedError(Exception):
    """Raised inside ``_resolve`` when a source holds more links than are stored."""


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _RawEdge:
    source: NodeKey | None
    target: NodeKey | None
    kind: str
    label: str | None = None
    relation_type: str | None = None
    via_journal_id: int | None = None
    # For a link whose target does not exist: what it names, of which type, in which project.
    broken_ref: str | None = None
    broken_project_id: int | None = None
    broken_target_type: str | None = None


@dataclass(slots=True)
class LinkNode:
    ref: str
    type: str
    title: str
    status: str | None
    url: str | None
    project: str | None = None


@dataclass(slots=True)
class LinkEdge:
    source: str
    target: str
    kind: str
    direction: str | None
    label: str | None = None
    relation_type: str | None = None
    via: str | None = None
    broken: bool = False
    # Where the comment a link is written in is shown; for the web page, not the JSON.
    via_url: str | None = None


@dataclass(slots=True)
class LinkGraph:
    root: LinkNode | None = None
    nodes: list[LinkNode] = field(default_factory=list)
    edges: list[LinkEdge] = field(default_factory=list)
    truncated: bool = False
    raw_edges: list[tuple[_RawEdge, bool]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": _node_dict(self.root) if self.root else None,
            "nodes": [_node_dict(n) for n in self.nodes],
            "edges": [_edge_dict(e) for e in self.edges],
            "truncated": self.truncated,
        }


def _node_dict(node: LinkNode) -> dict[str, Any]:
    return {
        "ref": node.ref,
        "type": node.type,
        "title": node.title,
        "status": node.status,
        "url": node.url,
        "project": node.project,
    }


def _edge_dict(edge: LinkEdge) -> dict[str, Any]:
    out: dict[str, Any] = {
        "source": edge.source,
        "target": edge.target,
        "kind": edge.kind,
        "direction": edge.direction,
    }
    for name in ("label", "relation_type", "via"):
        value = getattr(edge, name)
        if value:
            out[name] = value
    if edge.broken:
        out["broken"] = True
    return out


def _target_node(link: EntityLink) -> NodeKey | None:
    if link.target_issue_id is not None:
        return ("issue", link.target_issue_id)
    if link.target_wiki_page_id is not None:
        return ("wiki_page", link.target_wiki_page_id)
    if link.target_attachment_id is not None:
        return ("attachment", link.target_attachment_id)
    return None


def _broken_ref(link: EntityLink) -> str:
    if link.target_type == TARGET_ISSUE:
        return link.target_ref
    if link.target_type == TARGET_ATTACHMENT:
        return f"file:{link.target_ref}" if link.kind == KIND_ATTACHMENT_LINK else f"attachment:{link.target_ref}"
    return f"wiki:{{project}}/{link.target_ref}"


class _Reader:
    """Decides what one reader may see, in batches, and describes the nodes."""

    def __init__(self, session: AsyncSession, user: User) -> None:
        self.session = session
        self.user = user
        self._issues: dict[int, bool] = {}
        self._pages: dict[int, bool] = {}
        self._journals: dict[int, bool] = {}
        self._attachments: dict[int, bool] = {}
        self._commits: dict[int, bool] = {}
        self._pull_requests: dict[int, bool] = {}
        self._issue_projects: set[int] = set()
        self._wiki_projects: set[int] = set()
        self._scopes_loaded: set[int] = set()

    async def filter(self, edges: list[_RawEdge]) -> list[_RawEdge]:
        nodes: dict[str, set[int]] = defaultdict(set)
        journals: set[int] = set()
        for edge in edges:
            for endpoint in (edge.source, edge.target):
                if endpoint is not None:
                    nodes[endpoint[0]].add(endpoint[1])
            if edge.via_journal_id is not None:
                journals.add(edge.via_journal_id)
        await self._load_journals(journals)
        await self._load_attachments(nodes.get("attachment", set()))
        # Attachments and commits make the visibility of their containers matter too.
        await self._load_issues(nodes.get("issue", set()))
        await self._load_pages(nodes.get("wiki_page", set()))
        await self._load_development("commit", nodes.get("commit", set()))
        await self._load_development("pull_request", nodes.get("pull_request", set()))
        await self._load_broken_scopes({e.broken_project_id for e in edges if e.broken_ref is not None})
        return [
            e
            for e in edges
            if (e.source is None or self.visible(e.source))
            and (e.target is not None and self.visible(e.target) or e.target is None and self._broken_visible(e))
            and (e.via_journal_id is None or self._journals.get(e.via_journal_id, False))
        ]

    def _broken_visible(self, edge: _RawEdge) -> bool:
        """Whether a link to a missing target may be shown: only where the reader could see the target.

        A missing ``ACME-99`` is shown only to a reader who may view issues
        in ACME, and a missing wiki page only to one who may read ACME's wiki.
        Anyone else would otherwise learn that the project exists (a key of
        an unknown project is never stored) or, by comparing with the links
        to its existing issues, which are left out, which of them exist. So
        for them a missing target is left out exactly like a hidden one. An
        attachment named by id has no project to judge by and is left out.
        """
        if edge.broken_ref is None:
            return False
        if is_global_admin(self.user):
            return True
        project_id = edge.broken_project_id
        if project_id is None:
            return False
        if edge.broken_target_type == TARGET_ISSUE:
            return project_id in self._issue_projects
        if edge.broken_target_type == TARGET_WIKI_PAGE:
            return project_id in self._wiki_projects
        return project_id in self._issue_projects or project_id in self._wiki_projects

    async def _load_broken_scopes(self, project_ids: set[int | None]) -> None:
        """Record in which of *project_ids* the reader may view issues, and read the wiki."""
        from specivo.services.permission_service import Permission, projects_granting

        wanted = {p for p in project_ids if p is not None} - self._scopes_loaded
        if not wanted or is_global_admin(self.user):
            return
        projects = list((await self.session.execute(select(Project).where(Project.id.in_(wanted)))).scalars())
        self._issue_projects |= await projects_granting(self.session, self.user, projects, Permission.VIEW_ISSUES)
        wiki_enabled = set(
            (
                await self.session.execute(
                    select(EnabledModule.project_id).where(
                        EnabledModule.project_id.in_(wanted), EnabledModule.name == "wiki"
                    )
                )
            ).scalars()
        )
        self._wiki_projects |= wiki_enabled & await projects_granting(
            self.session, self.user, projects, Permission.VIEW_WIKI
        )
        self._scopes_loaded |= wanted

    def visible(self, node: NodeKey) -> bool:
        node_type, node_id = node
        table = {
            "issue": self._issues,
            "wiki_page": self._pages,
            "attachment": self._attachments,
            "commit": self._commits,
            "pull_request": self._pull_requests,
        }[node_type]
        return table.get(node_id, False)

    async def _load_issues(self, ids: set[int]) -> None:
        ids = ids - self._issues.keys()
        if not ids:
            return
        from specivo.services.issue_service import IssueService

        stmt = select(Issue.id).where(Issue.id.in_(ids))
        clause = await IssueService().visible_issues_clause(self.session, self.user)
        if clause is not None:
            stmt = stmt.where(clause)
        visible = set((await self.session.execute(stmt)).scalars().all())
        for issue_id in ids:
            self._issues[issue_id] = issue_id in visible

    async def _load_pages(self, ids: set[int]) -> None:
        ids = ids - self._pages.keys()
        if not ids:
            return
        from specivo.services.permission_service import Permission, projects_granting

        rows = (
            await self.session.execute(
                select(WikiPage.id, Project)
                .join(Wiki, Wiki.id == WikiPage.wiki_id)
                .join(Project, Project.id == Wiki.project_id)
                .join(EnabledModule, and_(EnabledModule.project_id == Project.id, EnabledModule.name == "wiki"))
                .where(WikiPage.id.in_(ids), WikiPage.deleted_at.is_(None))
            )
        ).all()
        projects = {project.id: project for _, project in rows}
        granted = await projects_granting(self.session, self.user, projects.values(), Permission.VIEW_WIKI)
        visible = {page_id for page_id, project in rows if project.id in granted}
        for page_id in ids:
            self._pages[page_id] = page_id in visible

    async def _load_journals(self, ids: set[int]) -> None:
        ids = ids - self._journals.keys()
        if not ids:
            return
        # Private notes are shown to admins only, as on the issue page and in
        # the API, and to an admin's project-limited key only in its projects.
        private: dict[int, int] = {
            journal_id: project_id
            for journal_id, project_id in await self.session.execute(
                select(Journal.id, Journal.project_id).where(Journal.id.in_(ids), Journal.is_private.is_(True))
            )
        }
        for journal_id in ids:
            self._journals[journal_id] = journal_id not in private or admin_reaches(self.user, private[journal_id])

    async def _load_attachments(self, ids: set[int]) -> None:
        ids = ids - self._attachments.keys()
        if not ids:
            return
        if self.user.is_anonymous:
            # The API leaves attachments out of every anonymous response.
            for attachment_id in ids:
                self._attachments[attachment_id] = False
            return
        rows = (
            await self.session.execute(
                select(Attachment.id, Attachment.container_type, Attachment.container_id).where(Attachment.id.in_(ids))
            )
        ).all()
        journal_ids = {cid for _, ctype, cid in rows if ctype == "Journal"}
        journal_parent: dict[int, tuple[int | None, int, bool]] = {}
        if journal_ids:
            for jid, issue_id, project_id, is_private in await self.session.execute(
                select(Journal.id, Journal.issue_id, Journal.project_id, Journal.is_private).where(
                    Journal.id.in_(journal_ids)
                )
            ):
                journal_parent[jid] = (issue_id, project_id, is_private)
        await self._load_issues(
            {cid for _, ctype, cid in rows if ctype == "Issue"}
            | {p[0] for p in journal_parent.values() if p[0] is not None}
        )
        await self._load_pages({cid for _, ctype, cid in rows if ctype == "WikiPage"})
        for attachment_id, ctype, cid in rows:
            if ctype == "Issue":
                ok = self._issues.get(cid, False)
            elif ctype == "WikiPage":
                ok = self._pages.get(cid, False)
            elif ctype == "Journal" and cid in journal_parent:
                issue_id, journal_project_id, is_private = journal_parent[cid]
                # As for downloads (``AttachmentService.can_view_container``):
                # a comment's file follows its issue, a private note's is for
                # admins only, and files on wiki comments are for admins only;
                # an admin's project-limited key reaches only its projects.
                admin = admin_reaches(self.user, journal_project_id)
                if issue_id is None:
                    ok = admin
                else:
                    ok = self._issues.get(issue_id, False) and (not is_private or admin)
            else:
                ok = False
            self._attachments[attachment_id] = ok
        for attachment_id in ids:
            self._attachments.setdefault(attachment_id, False)

    async def _load_development(self, node_type: str, ids: set[int]) -> None:
        """A commit or pull request is visible when one of the issues it is linked to is."""
        table = self._commits if node_type == "commit" else self._pull_requests
        ids = ids - table.keys()
        if not ids:
            return
        from specivo.models.repository import IssueCommitLink, IssuePullRequestLink

        if node_type == "commit":
            pairs = (
                await self.session.execute(
                    select(IssueCommitLink.commit_id, IssueCommitLink.issue_id).where(
                        IssueCommitLink.commit_id.in_(ids)
                    )
                )
            ).all()
        else:
            pairs = (
                await self.session.execute(
                    select(IssuePullRequestLink.pull_request_id, IssuePullRequestLink.issue_id).where(
                        IssuePullRequestLink.pull_request_id.in_(ids)
                    )
                )
            ).all()
        await self._load_issues({issue_id for _, issue_id in pairs})
        for item_id, issue_id in pairs:
            if self._issues.get(issue_id, False):
                table[item_id] = True
        for item_id in ids:
            table.setdefault(item_id, False)

    async def describe(self, graph: LinkGraph, root: NodeKey, nodes: list[NodeKey]) -> None:
        """Fill in the nodes (in the order found) and edges of *graph* from its raw edges."""
        refs = await self._describe_nodes(set(nodes))
        graph.root = refs.get(root)
        graph.nodes = [refs[n] for n in nodes if n != root and n in refs]
        journal_refs = await self._journal_refs({e.via_journal_id for e, _ in graph.raw_edges if e.via_journal_id})
        broken_projects = await self._project_keys({e.broken_project_id for e, _ in graph.raw_edges if e.broken_ref})
        root_ref = graph.root.ref if graph.root else None
        for raw, adjacent in graph.raw_edges:
            source = refs[raw.source].ref if raw.source in refs else None
            if raw.target is not None:
                target = refs[raw.target].ref if raw.target in refs else None
            elif raw.broken_ref is not None:
                target = raw.broken_ref.replace("{project}", broken_projects.get(raw.broken_project_id or 0, "?"))
            else:
                target = None
            if source is None or target is None:
                continue
            direction = None
            if adjacent and root_ref is not None:
                direction = "out" if source == root_ref else "in"
            graph.edges.append(
                LinkEdge(
                    source=source,
                    target=target,
                    kind=raw.kind,
                    direction=direction,
                    label=raw.label,
                    relation_type=raw.relation_type,
                    via=journal_refs[raw.via_journal_id][0] if raw.via_journal_id in journal_refs else None,
                    via_url=journal_refs[raw.via_journal_id][1] if raw.via_journal_id in journal_refs else None,
                    broken=raw.target is None,
                )
            )
        graph.raw_edges = []

    async def _describe_nodes(self, nodes: set[NodeKey]) -> dict[NodeKey, LinkNode]:
        by_type: dict[str, set[int]] = defaultdict(set)
        for node_type, node_id in nodes:
            by_type[node_type].add(node_id)
        out: dict[NodeKey, LinkNode] = {}
        if by_type["issue"]:
            for issue_id, key, subject, status, project_key in await self.session.execute(
                select(
                    Issue.id,
                    func.concat(Issue.project_key, "-", Issue.sequence_number),
                    Issue.subject,
                    IssueStatus.name,
                    Issue.project_key,
                )
                .join(IssueStatus, IssueStatus.id == Issue.status_id)
                .where(Issue.id.in_(by_type["issue"]))
            ):
                out[("issue", issue_id)] = LinkNode(key, "issue", subject, status, f"/issue/{key}/", project_key)
        if by_type["wiki_page"]:
            for page_id, slug, title, project_key in await self.session.execute(
                select(WikiPage.id, WikiPage.slug, WikiPage.title, Project.key)
                .join(Wiki, Wiki.id == WikiPage.wiki_id)
                .join(Project, Project.id == Wiki.project_id)
                .where(WikiPage.id.in_(by_type["wiki_page"]))
            ):
                out[("wiki_page", page_id)] = LinkNode(
                    f"wiki:{project_key}/{slug}",
                    "wiki_page",
                    title,
                    None,
                    f"/projects/{project_key}/wiki/{slug}/",
                    project_key,
                )
        if by_type["attachment"]:
            for attachment_id, filename in await self.session.execute(
                select(Attachment.id, Attachment.filename).where(Attachment.id.in_(by_type["attachment"]))
            ):
                out[("attachment", attachment_id)] = LinkNode(
                    f"attachment:{attachment_id}",
                    "attachment",
                    filename,
                    None,
                    f"/api/v1/attachments/{attachment_id}/download/",
                )
        if by_type["commit"] or by_type["pull_request"]:
            from specivo.models.repository import PullRequest, RepositoryCommit

            for commit_id, sha, message, url in await self.session.execute(
                select(RepositoryCommit.id, RepositoryCommit.sha, RepositoryCommit.message, RepositoryCommit.url).where(
                    RepositoryCommit.id.in_(by_type["commit"])
                )
            ):
                first = (message or "").splitlines()[0] if message else ""
                out[("commit", commit_id)] = LinkNode(f"commit:{sha[:12]}", "commit", first, None, url or None)
            for pr_id, number, title, url, state in await self.session.execute(
                select(PullRequest.id, PullRequest.number, PullRequest.title, PullRequest.url, PullRequest.state).where(
                    PullRequest.id.in_(by_type["pull_request"])
                )
            ):
                out[("pull_request", pr_id)] = LinkNode(
                    f"pr:{pr_id}", "pull_request", f"#{number} {title}".strip(), state, url or None
                )
        return out

    async def _journal_refs(self, ids: set[int | None]) -> dict[int, tuple[str, str]]:
        """``journal id -> (ref, url)``: ``ACME-12#3`` and the comment's anchor on the issue page."""
        wanted = {i for i in ids if i is not None}
        if not wanted:
            return {}
        out: dict[int, tuple[str, str]] = {}
        rows = await self.session.execute(
            select(
                Journal.id,
                Journal.sequence,
                func.concat(Issue.project_key, "-", Issue.sequence_number),
                Project.key,
                WikiPage.slug,
            )
            .outerjoin(Issue, Issue.id == Journal.issue_id)
            .outerjoin(WikiPage, WikiPage.id == Journal.wiki_page_id)
            .outerjoin(Wiki, Wiki.id == WikiPage.wiki_id)
            .outerjoin(Project, Project.id == Wiki.project_id)
            .where(Journal.id.in_(wanted))
        )
        for journal_id, sequence, issue_key, project_key, slug in rows:
            if slug is not None:
                out[journal_id] = (f"wiki:{project_key}/{slug}#{sequence}", f"/projects/{project_key}/wiki/{slug}/")
            else:
                out[journal_id] = (f"{issue_key}#{sequence}", f"/issue/{issue_key}/#journal-{journal_id}")
        return out

    async def _project_keys(self, ids: set[int | None]) -> dict[int, str]:
        wanted = {i for i in ids if i is not None}
        if not wanted:
            return {}
        return {
            pid: key
            for pid, key in await self.session.execute(select(Project.id, Project.key).where(Project.id.in_(wanted)))
        }


async def resolve_root(session: AsyncSession, user: User, ref: str) -> NodeKey:
    """Resolve an issue (``ACME-12``, ``issue:ACME-12``) or wiki page (``wiki:ACME/slug``) ref *user* may see.

    An issue the user may not see is not found (404), as everywhere else; a
    wiki the user may not read is refused like the wiki pages themselves.
    """
    from specivo.core.exceptions import NotFoundError, PermissionDeniedError
    from specivo.services.issue_service import IssueService
    from specivo.services.permission_service import Permission, check_permission
    from specivo.services.project_service import ProjectService
    from specivo.services.wiki_service import WikiService

    ref = ref.strip()
    if ref.lower().startswith("wiki:"):
        rest = ref[len("wiki:") :]
        if "/" not in rest:
            raise ValueError("A wiki ref is 'wiki:PROJECT_KEY/slug'")
        project_key, slug = rest.split("/", 1)
        project_service = ProjectService()
        project = await project_service.get_by_key(session, project_key.upper())
        await project_service.require_project_access(session, project, user)
        enabled = await session.scalar(
            select(EnabledModule.id).where(EnabledModule.project_id == project.id, EnabledModule.name == "wiki")
        )
        if enabled is None:
            raise NotFoundError(f"Wiki page '{slug}' not found")
        if not await check_permission(user, project.id, Permission.VIEW_WIKI, session):
            raise PermissionDeniedError("You do not have permission to view wiki pages")
        page, _content = await WikiService().get_page(session, project.id, slug.strip("/"))
        return ("wiki_page", page.id)
    if ref.lower().startswith("issue:"):
        ref = ref[len("issue:") :]
    issue = await IssueService().get_by_display_key(session, ref.upper(), user=user)
    if not await check_permission(user, issue.project_id, Permission.VIEW_ISSUES, session):
        raise NotFoundError(f"Issue {ref!r} not found")
    return ("issue", issue.id)
