"""Suggested links: document vectors, the nearest neighbours they suggest, and accepting or dismissing them.

Vectors
-------
:func:`refresh_vectors` gives every issue and wiki page whose chunks carry
embeddings of a model one document vector: the mean of those chunk
embeddings, L2-normalised (``l2_normalize(avg(embedding))``, one statement).
The embedding task calls it for the sources it has just embedded, and the
nightly sweep for a whole project, so vectors follow the chunks they come from.

Suggestions
-----------
:func:`compute_for_project` (the sweep) and :func:`compute_for_items` (after a
source was embedded again) store, per item, its ``SIMILAR_LINKS_TOP_K``
nearest neighbours in the same project with the same model whose cosine
similarity reaches the project's threshold. Pairs already linked by any stored
or structured edge (a text link, a typed link, a relation, parent and child)
and pairs someone accepted or dismissed are skipped, so a dismissed pair never
comes back for that model. The neighbour search is exact (no vector index):
one project's items at a time.

Reading
-------
:func:`pending_suggestions` lists the open suggestions of one item for the
link graph (edge kind ``similar``); pairs linked since the last computation
are left out there too. Visibility is the link graph reader's: a suggestion is
shown only when the reader may see both items.

Deciding
--------
:func:`decide` accepts or dismisses one suggestion. Accepting two issues makes
an ``IssueRelation`` (``manage_issue_relations``, as for any relation);
accepting an issue and a page, or two pages, makes a stored ``typed_link``
from the first item to the other (``edit_issues`` on an issue,
``manage_wiki`` on a page, the permission that edits the source). Dismissing
asks for the same permission as accepting would.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Integer, and_, delete, exists, false, func, literal, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import (
    SIMILAR_LINKS_CANDIDATE_MARGIN,
    SIMILAR_LINKS_DEFAULT_THRESHOLD,
    SIMILAR_LINKS_MAX_THRESHOLD,
    SIMILAR_LINKS_MIN_THRESHOLD,
    SIMILAR_LINKS_TOP_K,
)
from specivo.core.i18n import gettext as _
from specivo.models.entity_link import KIND_TYPED_LINK, TYPED_LINK_TYPES, EntityLink
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.link_suggestion import (
    ITEM_ISSUE,
    ITEM_TYPES,
    ITEM_WIKI_PAGE,
    STATUS_ACCEPTED,
    STATUS_DISMISSED,
    STATUS_SUGGESTED,
    EntitySuggestion,
    EntityVector,
    pair_key,
)
from specivo.models.project import Project
from specivo.models.relation import IssueRelation
from specivo.models.search import ChunkEmbedding, EmbeddingModel, SearchChunk, SearchSource
from specivo.models.user import User
from specivo.models.wiki import Wiki, WikiPage

logger = logging.getLogger(__name__)

Item = tuple[str, int]
Pair = tuple[Item, Item]

#: Project settings key: the cosine similarity a suggestion needs in this project.
THRESHOLD_SETTING = "similar_links_threshold"

#: First key of the two-key advisory lock taken per project while its suggestions are computed.
_COMPUTE_LOCK_NAMESPACE = 0x5E50

#: Values per ``IN (...)``.
_BATCH = 500

OP_ACCEPT = "accept"
OP_DISMISS = "dismiss"
OPS = (OP_ACCEPT, OP_DISMISS)


def similarity_threshold(settings: dict[str, Any] | None) -> float:
    """The cosine similarity a suggestion needs in a project with these *settings*."""
    raw = (settings or {}).get(THRESHOLD_SETTING)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return SIMILAR_LINKS_DEFAULT_THRESHOLD
    return min(max(float(raw), SIMILAR_LINKS_MIN_THRESHOLD), SIMILAR_LINKS_MAX_THRESHOLD)


async def default_model_id(session: AsyncSession) -> int | None:
    """The id of the default embedding model, whose vectors suggestions are made from."""
    return await session.scalar(select(EmbeddingModel.id).where(EmbeddingModel.is_default.is_(True)).limit(1))


# ---------------------------------------------------------------------------
# Document vectors
# ---------------------------------------------------------------------------


def _source_scope(project_id: int | None, items: Iterable[Item] | None) -> Any:
    clauses: list[Any] = [SearchSource.source_type.in_(ITEM_TYPES)]
    if project_id is not None:
        clauses.append(SearchSource.project_id == project_id)
    if items is not None:
        wanted = sorted(set(items))
        clauses.append(tuple_(SearchSource.source_type, SearchSource.entity_id).in_(wanted) if wanted else false())
    return and_(*clauses)


async def refresh_vectors(
    session: AsyncSession, model_id: int, *, project_id: int | None = None, items: Iterable[Item] | None = None
) -> set[Item]:
    """Bring the document vectors of a project's (or of *items*') issues and pages in line with their chunks.

    An item with at least one chunk embedded by *model_id* gets the mean of
    those embeddings, L2-normalised; one without loses its vector. Returns the
    items whose vector was written, changed or removed.
    """
    item_list = None if items is None else list(items)
    scope = _source_scope(project_id, item_list)
    mean = func.l2_normalize(func.avg(ChunkEmbedding.embedding), type_=Vector())
    vectors = (
        select(SearchSource.id, literal(model_id, Integer), mean)
        .join(SearchChunk, SearchChunk.source_id == SearchSource.id)
        .join(ChunkEmbedding, and_(ChunkEmbedding.chunk_id == SearchChunk.id, ChunkEmbedding.model_id == model_id))
        .where(scope)
        .group_by(SearchSource.id)
    )
    insert = pg_insert(EntityVector).from_select(["search_source_id", "model_id", "vector"], vectors)
    upsert = insert.on_conflict_do_update(
        index_elements=["search_source_id", "model_id"],
        set_={"vector": insert.excluded.vector, "updated_at": func.now()},
        where=EntityVector.vector.is_distinct_from(insert.excluded.vector),
    ).returning(EntityVector.search_source_id)
    changed_ids = set((await session.execute(upsert)).scalars())

    has_embedding = exists(
        select(ChunkEmbedding.id)
        .join(SearchChunk, SearchChunk.id == ChunkEmbedding.chunk_id)
        .where(SearchChunk.source_id == SearchSource.id, ChunkEmbedding.model_id == model_id)
    )
    stale = (
        select(EntityVector.id)
        .join(SearchSource, SearchSource.id == EntityVector.search_source_id)
        .where(EntityVector.model_id == model_id, scope, ~has_embedding)
    )
    removed = (
        await session.execute(
            delete(EntityVector)
            .where(EntityVector.id.in_(stale))
            .returning(EntityVector.search_source_id)
            .execution_options(synchronize_session=False)
        )
    ).scalars()
    changed_ids |= set(removed)
    if not changed_ids:
        return set()
    rows = await session.execute(
        select(SearchSource.source_type, SearchSource.entity_id).where(SearchSource.id.in_(changed_ids))
    )
    changed = {(t, i) for t, i in rows}
    # A removed vector's source may be gone (a deleted issue): report the items asked about too.
    if item_list is not None and len(changed) < len(changed_ids):
        changed |= set(item_list)
    return changed


# ---------------------------------------------------------------------------
# Pairs already linked, or already decided
# ---------------------------------------------------------------------------


def _ids(items: Iterable[Item], item_type: str) -> list[int]:
    return sorted({i for t, i in items if t == item_type})


async def linked_pairs(
    session: AsyncSession, *, project_id: int | None = None, items: Iterable[Item] | None = None
) -> set[Pair]:
    """Pairs of issues and pages joined by a stored link or a structured edge, touching the project or *items*.

    Stored: text links (in descriptions, comments and pages) and typed links
    between two items. Structured: relations, and parent and child (issues and
    pages). Shared tags, versions and other hubs do not count: they are not a
    link between the two.
    """
    item_list = [] if items is None else list(items)
    issue_ids, page_ids = _ids(item_list, ITEM_ISSUE), _ids(item_list, ITEM_WIKI_PAGE)
    if project_id is None and not issue_ids and not page_ids:
        return set()
    pairs = await _stored_link_pairs(session, project_id, issue_ids, page_ids)
    pairs |= await _structured_pairs(session, project_id, issue_ids, page_ids)
    return pairs


def _link_source_item(
    src_issue: int | None, src_page: int | None, j_issue: int | None, j_page: int | None
) -> Item | None:
    """The item whose text holds a link: its issue or page, or the issue or page of its comment."""
    if src_issue is not None:
        return (ITEM_ISSUE, src_issue)
    if src_page is not None:
        return (ITEM_WIKI_PAGE, src_page)
    if j_issue is not None:
        return (ITEM_ISSUE, j_issue)
    if j_page is not None:
        return (ITEM_WIKI_PAGE, j_page)
    return None


async def _stored_link_pairs(
    session: AsyncSession, project_id: int | None, issue_ids: list[int], page_ids: list[int]
) -> set[Pair]:
    stmt = (
        select(
            EntityLink.source_issue_id,
            EntityLink.source_wiki_page_id,
            Journal.issue_id,
            Journal.wiki_page_id,
            EntityLink.target_issue_id,
            EntityLink.target_wiki_page_id,
        )
        .outerjoin(Journal, Journal.id == EntityLink.source_journal_id)
        .where(or_(EntityLink.target_issue_id.is_not(None), EntityLink.target_wiki_page_id.is_not(None)))
    )
    if project_id is not None:
        stmt = stmt.where(or_(EntityLink.source_project_id == project_id, EntityLink.target_project_id == project_id))
    else:
        conditions = []
        if issue_ids:
            conditions += [
                EntityLink.source_issue_id.in_(issue_ids),
                Journal.issue_id.in_(issue_ids),
                EntityLink.target_issue_id.in_(issue_ids),
            ]
        if page_ids:
            conditions += [
                EntityLink.source_wiki_page_id.in_(page_ids),
                Journal.wiki_page_id.in_(page_ids),
                EntityLink.target_wiki_page_id.in_(page_ids),
            ]
        stmt = stmt.where(or_(*conditions))
    pairs: set[Pair] = set()
    for src_issue, src_page, j_issue, j_page, dst_issue, dst_page in await session.execute(stmt):
        source = _link_source_item(src_issue, src_page, j_issue, j_page)
        target: Item = (ITEM_ISSUE, int(dst_issue)) if dst_issue is not None else (ITEM_WIKI_PAGE, int(dst_page or 0))
        if source is not None and source != target:
            pairs.add(pair_key(source, target))
    return pairs


async def _structured_pairs(
    session: AsyncSession, project_id: int | None, issue_ids: list[int], page_ids: list[int]
) -> set[Pair]:
    """Relations and parent/child between issues, and parent/child between wiki pages."""
    pairs: set[Pair] = set()
    issue_scope: Any = select(Issue.id).where(Issue.project_id == project_id) if project_id is not None else issue_ids
    if project_id is not None or issue_ids:
        relations = select(IssueRelation.issue_from_id, IssueRelation.issue_to_id).where(
            or_(IssueRelation.issue_from_id.in_(issue_scope), IssueRelation.issue_to_id.in_(issue_scope))
        )
        for a, b in await session.execute(relations):
            pairs.add(pair_key((ITEM_ISSUE, a), (ITEM_ISSUE, b)))
        hierarchy = select(Issue.id, Issue.parent_id).where(
            Issue.parent_id.is_not(None), or_(Issue.id.in_(issue_scope), Issue.parent_id.in_(issue_scope))
        )
        for child, parent in await session.execute(hierarchy):
            pairs.add(pair_key((ITEM_ISSUE, child), (ITEM_ISSUE, int(parent or 0))))
    page_scope: Any = (
        select(WikiPage.id).join(Wiki, Wiki.id == WikiPage.wiki_id).where(Wiki.project_id == project_id)
        if project_id is not None
        else page_ids
    )
    if project_id is not None or page_ids:
        hierarchy = select(WikiPage.id, WikiPage.parent_id).where(
            WikiPage.parent_id.is_not(None), or_(WikiPage.id.in_(page_scope), WikiPage.parent_id.in_(page_scope))
        )
        for child, parent in await session.execute(hierarchy):
            pairs.add(pair_key((ITEM_WIKI_PAGE, child), (ITEM_WIKI_PAGE, int(parent or 0))))
    return pairs


def _row_pair(row: EntitySuggestion) -> Pair:
    return (row.source, row.target)


def _involving(items: Iterable[Item]) -> Any:
    item_list = list(items)
    issue_ids, page_ids = _ids(item_list, ITEM_ISSUE), _ids(item_list, ITEM_WIKI_PAGE)
    conditions = []
    if issue_ids:
        conditions += [EntitySuggestion.source_issue_id.in_(issue_ids), EntitySuggestion.target_issue_id.in_(issue_ids)]
    if page_ids:
        conditions += [
            EntitySuggestion.source_wiki_page_id.in_(page_ids),
            EntitySuggestion.target_wiki_page_id.in_(page_ids),
        ]
    return or_(*conditions) if conditions else false()


async def _decided_pairs(
    session: AsyncSession, model_id: int, *, project_id: int | None = None, items: Iterable[Item] | None = None
) -> set[Pair]:
    """Pairs accepted or dismissed for *model_id* that touch the project's items or *items*."""
    stmt = select(EntitySuggestion).where(
        EntitySuggestion.model_id == model_id, EntitySuggestion.status != STATUS_SUGGESTED
    )
    if project_id is not None:
        issues = select(Issue.id).where(Issue.project_id == project_id)
        pages = select(WikiPage.id).join(Wiki, Wiki.id == WikiPage.wiki_id).where(Wiki.project_id == project_id)
        stmt = stmt.where(
            or_(
                EntitySuggestion.project_id == project_id,
                EntitySuggestion.source_issue_id.in_(issues),
                EntitySuggestion.target_issue_id.in_(issues),
                EntitySuggestion.source_wiki_page_id.in_(pages),
                EntitySuggestion.target_wiki_page_id.in_(pages),
            )
        )
    else:
        stmt = stmt.where(_involving(items or []))
    return {_row_pair(row) for row in (await session.execute(stmt)).scalars()}


# ---------------------------------------------------------------------------
# Computing suggestions
# ---------------------------------------------------------------------------

# For each item of the scope, its nearest neighbours among the issues and pages
# of its own (current) project, with the same model. Exact: no vector index.
_NEIGHBOURS_SQL = """
SELECT ss1.source_type AS item_type, ss1.entity_id AS item_id, ss1.project_id AS project_id,
       nb.source_type AS other_type, nb.entity_id AS other_id, nb.score AS score
FROM entity_vectors ev1
JOIN search_sources ss1 ON ss1.id = ev1.search_source_id
CROSS JOIN LATERAL (
    SELECT ss2.source_type, ss2.entity_id, 1 - (ev2.vector <=> ev1.vector) AS score
    FROM entity_vectors ev2
    JOIN search_sources ss2 ON ss2.id = ev2.search_source_id
    WHERE ev2.model_id = ev1.model_id
      AND ev2.id <> ev1.id
      AND ss2.project_id = ss1.project_id
      AND ss2.source_type IN ('issue', 'wiki_page')
    ORDER BY ev2.vector <=> ev1.vector
    LIMIT :candidates
) nb
WHERE ev1.model_id = :model_id
  AND ss1.source_type IN ('issue', 'wiki_page')
  AND {scope}
ORDER BY ss1.project_id, ss1.source_type, ss1.entity_id, nb.score DESC
"""


@dataclass(slots=True)
class _Neighbour:
    item: Item
    project_id: int
    other: Item
    score: float


async def _neighbours(
    session: AsyncSession, model_id: int, *, project_id: int | None = None, items: Iterable[Item] | None = None
) -> list[_Neighbour]:
    params: dict[str, Any] = {
        "model_id": model_id,
        "candidates": SIMILAR_LINKS_TOP_K + SIMILAR_LINKS_CANDIDATE_MARGIN,
    }
    if project_id is not None:
        scope = "ss1.project_id = :project_id"
        params["project_id"] = project_id
    else:
        item_list = sorted(set(items or []))
        issue_ids, page_ids = _ids(item_list, ITEM_ISSUE), _ids(item_list, ITEM_WIKI_PAGE)
        if not issue_ids and not page_ids:
            return []
        scope = (
            "((ss1.source_type = 'issue' AND ss1.entity_id = ANY(:issue_ids))"
            " OR (ss1.source_type = 'wiki_page' AND ss1.entity_id = ANY(:page_ids)))"
        )
        params["issue_ids"] = issue_ids
        params["page_ids"] = page_ids
    rows = await session.execute(text(_NEIGHBOURS_SQL.format(scope=scope)), params)
    return [
        _Neighbour((r.item_type, r.item_id), r.project_id, (r.other_type, r.other_id), float(r.score)) for r in rows
    ]


async def _thresholds(session: AsyncSession, project_ids: Iterable[int]) -> dict[int, float]:
    wanted = sorted(set(project_ids))
    if not wanted:
        return {}
    rows = await session.execute(select(Project.id, Project.settings).where(Project.id.in_(wanted)))
    return {pid: similarity_threshold(settings) for pid, settings in rows}


async def _lock_project(session: AsyncSession, project_id: int) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(_COMPUTE_LOCK_NAMESPACE, project_id)))


def _pick(neighbours: list[_Neighbour], thresholds: dict[int, float], skip: set[Pair]) -> dict[Pair, tuple[float, int]]:
    """The top-k admissible neighbours of each item, as ``pair -> (score, project id)``."""
    chosen: dict[Pair, tuple[float, int]] = {}
    taken: dict[Item, int] = defaultdict(int)
    for nb in neighbours:
        if taken[nb.item] >= SIMILAR_LINKS_TOP_K:
            continue
        if nb.score < thresholds.get(nb.project_id, SIMILAR_LINKS_DEFAULT_THRESHOLD):
            continue
        pair = pair_key(nb.item, nb.other)
        if pair in skip:
            continue
        taken[nb.item] += 1
        if pair not in chosen or chosen[pair][0] < nb.score:
            chosen[pair] = (nb.score, nb.project_id)
    return chosen


async def _store(session: AsyncSession, model_id: int, chosen: dict[Pair, tuple[float, int]]) -> int:
    rows = []
    for (source, target), (score, project_id) in chosen.items():
        rows.append(
            {
                "project_id": project_id,
                "model_id": model_id,
                "source_issue_id": source[1] if source[0] == ITEM_ISSUE else None,
                "source_wiki_page_id": source[1] if source[0] == ITEM_WIKI_PAGE else None,
                "target_issue_id": target[1] if target[0] == ITEM_ISSUE else None,
                "target_wiki_page_id": target[1] if target[0] == ITEM_WIKI_PAGE else None,
                # Rounding keeps a score of 1.0000001 (float error on identical vectors) inside the CHECK.
                "score": max(-1.0, min(1.0, round(score, 6))),
                "status": STATUS_SUGGESTED,
            }
        )
    stored = 0
    for i in range(0, len(rows), _BATCH):
        result = await session.execute(
            pg_insert(EntitySuggestion).values(rows[i : i + _BATCH]).on_conflict_do_nothing()
        )
        stored += int(getattr(result, "rowcount", 0) or 0)
    return stored


async def compute_for_project(session: AsyncSession, project_id: int, model_id: int | None = None) -> int:
    """Recompute every open suggestion of a project from fresh vectors; return how many are stored.

    Open suggestions of the project (of any model) are replaced; accepted and
    dismissed pairs stay as they are. Holds the project's compute lock until
    the caller's transaction ends.
    """
    model_id = model_id if model_id is not None else await default_model_id(session)
    if model_id is None:
        return 0
    await _lock_project(session, project_id)
    await refresh_vectors(session, model_id, project_id=project_id)
    await session.execute(
        delete(EntitySuggestion)
        .where(EntitySuggestion.project_id == project_id, EntitySuggestion.status == STATUS_SUGGESTED)
        .execution_options(synchronize_session=False)
    )
    neighbours = await _neighbours(session, model_id, project_id=project_id)
    if not neighbours:
        return 0
    skip = await linked_pairs(session, project_id=project_id) | await _decided_pairs(
        session, model_id, project_id=project_id
    )
    chosen = _pick(neighbours, await _thresholds(session, [project_id]), skip)
    stored = await _store(session, model_id, chosen)
    logger.info("Stored %d suggested link(s) in project %d", stored, project_id)
    return stored


async def compute_for_items(session: AsyncSession, items: Iterable[Item], model_id: int | None = None) -> int:
    """Recompute the open suggestions of *items* (issues and pages embedded again); return how many are stored.

    Their open suggestions are replaced by their nearest neighbours now. An
    item that has no vector any more (deleted, in the trash, emptied) just
    loses its open suggestions.
    """
    item_list = sorted({(t, int(i)) for t, i in items if t in ITEM_TYPES})
    model_id = model_id if model_id is not None else await default_model_id(session)
    if model_id is None or not item_list:
        return 0
    projects = await session.execute(select(SearchSource.project_id).where(_source_scope(None, item_list)).distinct())
    for project_id in sorted(projects.scalars()):
        await _lock_project(session, project_id)
    await session.execute(
        delete(EntitySuggestion)
        .where(_involving(item_list), EntitySuggestion.status == STATUS_SUGGESTED)
        .execution_options(synchronize_session=False)
    )
    neighbours = await _neighbours(session, model_id, items=item_list)
    if not neighbours:
        return 0
    others = {nb.other for nb in neighbours}
    skip = await linked_pairs(session, items=item_list) | await _decided_pairs(
        session, model_id, items=set(item_list) | others
    )
    chosen = _pick(neighbours, await _thresholds(session, {nb.project_id for nb in neighbours}), skip)
    return await _store(session, model_id, chosen)


async def projects_with_vectors_or_embeddings(session: AsyncSession, model_id: int) -> list[int]:
    """Projects holding an issue or page with chunks embedded by *model_id*: the ones the sweep visits."""
    stmt = (
        select(SearchSource.project_id)
        .where(
            SearchSource.source_type.in_(ITEM_TYPES),
            exists(
                select(ChunkEmbedding.id)
                .join(SearchChunk, SearchChunk.id == ChunkEmbedding.chunk_id)
                .where(SearchChunk.source_id == SearchSource.id, ChunkEmbedding.model_id == model_id)
            ),
        )
        .distinct()
        .order_by(SearchSource.project_id)
    )
    return list((await session.execute(stmt)).scalars())


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PendingSuggestion:
    id: int
    other: Item
    score: float


async def pending_suggestions(session: AsyncSession, item: Item, model_id: int) -> list[PendingSuggestion]:
    """Open suggestions of *item* for *model_id*, best first, without pairs that were linked since.

    Not filtered for any reader: the link graph reader drops the pairs whose
    other end the reader may not see.
    """
    rows = (
        (
            await session.execute(
                select(EntitySuggestion)
                .where(
                    EntitySuggestion.model_id == model_id,
                    EntitySuggestion.status == STATUS_SUGGESTED,
                    _involving([item]),
                )
                .order_by(EntitySuggestion.score.desc(), EntitySuggestion.id)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    linked = await linked_pairs(session, items=[item])
    out: list[PendingSuggestion] = []
    for row in rows:
        pair = _row_pair(row)
        if pair in linked:
            continue
        other = row.target if row.source == item else row.source
        out.append(PendingSuggestion(row.id, other, row.score))
    return out


# ---------------------------------------------------------------------------
# Accepting and dismissing
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Decision:
    op: str
    status: str
    source_ref: str
    target_ref: str
    link_kind: str | None = None  # "relation" or "typed_link" when accepted
    link_type: str | None = None
    relation_id: int | None = None
    project_id: int | None = None

    def describe(self) -> str:
        if self.op == OP_DISMISS:
            return (
                f"Dismissed the suggested link {self.source_ref} ~ {self.target_ref}; it will not be suggested again."
            )
        if self.link_kind == "relation":
            return (
                f"Accepted: created relation #{self.relation_id}: {self.source_ref} {self.link_type} {self.target_ref}"
            )
        return f"Accepted: {self.source_ref} {self.link_type} {self.target_ref} (typed link)"


async def _item_info(session: AsyncSession, item: Item) -> tuple[int, str]:
    """``(project id, ref)`` of an issue or page: ``ACME-12`` or ``wiki:ACME/slug``."""
    if item[0] == ITEM_ISSUE:
        issue = await session.get(Issue, item[1])
        if issue is None:
            raise ValueError("Issue not found")
        return issue.project_id, issue.display_key
    row = (
        await session.execute(
            select(Wiki.project_id, WikiPage.slug, Project.key)
            .join(WikiPage, WikiPage.wiki_id == Wiki.id)
            .join(Project, Project.id == Wiki.project_id)
            .where(WikiPage.id == item[1])
        )
    ).one_or_none()
    if row is None:
        from specivo.core.exceptions import NotFoundError

        raise NotFoundError("Wiki page not found")
    return row.project_id, f"wiki:{row.key}/{row.slug}"


async def _require_decide_permission(
    session: AsyncSession, user: User, source: Item, target: Item, source_project_id: int, target_project_id: int
) -> None:
    """The permission that makes the link: a relation between issues, else an edit of the source.

    When the two items are in different projects (one was moved after the
    suggestion was made), the matching permission is needed on the target's
    project too, so nobody decides for a project where they may not link.
    """
    from specivo.core.exceptions import PermissionDeniedError
    from specivo.services.permission_service import Permission, check_permission

    def needed(item: Item) -> Permission:
        if source[0] == ITEM_ISSUE and target[0] == ITEM_ISSUE:
            return Permission.MANAGE_ISSUE_RELATIONS
        return Permission.EDIT_ISSUES if item[0] == ITEM_ISSUE else Permission.MANAGE_WIKI

    checks = [(source_project_id, needed(source))]
    if target_project_id != source_project_id:
        checks.append((target_project_id, needed(target)))
    for project_id, permission in checks:
        if not await check_permission(user, project_id, permission, session):
            raise PermissionDeniedError(f"Permission '{permission}' denied for this project")


async def _require_visible(session: AsyncSession, user: User, items: Iterable[Item]) -> None:
    """Refuse (not found) an item *user* may not see; callers resolve refs first, this is the backstop."""
    from specivo.core.exceptions import NotFoundError
    from specivo.services.entity_link_service import _Reader

    reader = _Reader(session, user)
    wanted = list(items)
    await reader._load_issues({i for t, i in wanted if t == ITEM_ISSUE})
    await reader._load_pages({i for t, i in wanted if t == ITEM_WIKI_PAGE})
    if not all(reader.visible(item) for item in wanted):
        raise NotFoundError("Item not found")


def check_link_type(source: Item, target: Item, link_type: str | None) -> str:
    """The link type an accept uses: a relation type for two issues, else a typed link type."""
    from specivo.core.exceptions import ValidationError
    from specivo.services.relation_service import RELATION_TYPES

    if source[0] == ITEM_ISSUE and target[0] == ITEM_ISSUE:
        value = link_type or "relates"
        if value not in RELATION_TYPES:
            raise ValidationError(
                f"Two issues are linked by a relation: link_type must be one of {', '.join(RELATION_TYPES)}",
                field="link_type",
            )
        return value
    value = link_type or "see_also"
    if value not in TYPED_LINK_TYPES:
        raise ValidationError(
            f"link_type must be one of {', '.join(TYPED_LINK_TYPES)}",
            field="link_type",
        )
    return value


async def decide(
    session: AsyncSession,
    user: User,
    op: str,
    source: Item,
    target: Item,
    *,
    link_type: str | None = None,
) -> Decision:
    """Accept or dismiss the open suggestion between *source* and *target* (both visible to *user*).

    *source* is the item the decision is made from: an accepted typed link
    goes from it to *target*, an accepted relation reads ``source link_type
    target``, and the permission is checked in its project.
    """
    from specivo.core.exceptions import NotFoundError, ValidationError
    from specivo.core.utils import utcnow

    if op not in OPS:
        raise ValidationError(f"op must be one of {', '.join(OPS)}", field="op")
    if source == target:
        raise ValidationError(_("An item cannot be linked to itself"), field="other_ref")
    if source[0] not in ITEM_TYPES or target[0] not in ITEM_TYPES:
        raise ValidationError(_("Suggested links join issues and wiki pages only"), field="other_ref")
    value = check_link_type(source, target, link_type) if op == OP_ACCEPT else ""

    source_project_id, source_ref = await _item_info(session, source)
    target_project_id, target_ref = await _item_info(session, target)
    await _require_visible(session, user, [source, target])
    await _require_decide_permission(session, user, source, target, source_project_id, target_project_id)

    model_id = await default_model_id(session)
    first, second = pair_key(source, target)
    row = None
    if model_id is not None:
        row = (
            await session.execute(
                select(EntitySuggestion)
                .where(
                    EntitySuggestion.model_id == model_id,
                    EntitySuggestion.source_issue_id == (first[1] if first[0] == ITEM_ISSUE else None),
                    EntitySuggestion.source_wiki_page_id == (first[1] if first[0] == ITEM_WIKI_PAGE else None),
                    EntitySuggestion.target_issue_id == (second[1] if second[0] == ITEM_ISSUE else None),
                    EntitySuggestion.target_wiki_page_id == (second[1] if second[0] == ITEM_WIKI_PAGE else None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
    if row is None or row.status != STATUS_SUGGESTED:
        if row is not None:
            if row.status == STATUS_ACCEPTED:
                message = _("The suggested link between %(source)s and %(target)s was already accepted")
            else:
                message = _("The suggested link between %(source)s and %(target)s was already dismissed")
            raise ValidationError(message % {"source": source_ref, "target": target_ref})
        raise NotFoundError(
            _("No suggested link between %(source)s and %(target)s") % {"source": source_ref, "target": target_ref}
        )

    decision = Decision(op=op, status="", source_ref=source_ref, target_ref=target_ref, project_id=source_project_id)
    if op == OP_ACCEPT:
        decision.link_type = value
        if source[0] == ITEM_ISSUE and target[0] == ITEM_ISSUE:
            from specivo.services.relation_service import RelationService

            issue_from = await session.get(Issue, source[1])
            issue_to = await session.get(Issue, target[1])
            if issue_from is None or issue_to is None:
                raise NotFoundError("Issue not found")
            relation = await RelationService().create(session, issue_from, issue_to, value, actor=user)
            decision.link_kind = "relation"
            decision.relation_id = relation.id
        else:
            await _add_typed_link(session, source, target, source_project_id, value, target_ref)
            decision.link_kind = KIND_TYPED_LINK
        row.status = STATUS_ACCEPTED
    else:
        row.status = STATUS_DISMISSED
    row.decided_by_id = user.id
    row.decided_at = utcnow()
    decision.status = row.status
    await session.flush()
    return decision


async def _add_typed_link(
    session: AsyncSession, source: Item, target: Item, source_project_id: int, link_type: str, target_ref: str
) -> EntityLink:
    from specivo.core.exceptions import ValidationError
    from specivo.services.entity_link_service import _lock_projects

    target_project_id, _target_ref = await _item_info(session, target)
    await _lock_projects(session, [source_project_id, target_project_id])
    # One typed link per pair, whichever way it points.
    ends = [
        and_(_source_column(source) == source[1], _target_column(target) == target[1]),
        and_(_source_column(target) == target[1], _target_column(source) == source[1]),
    ]
    existing = await session.scalar(
        select(EntityLink.id).where(EntityLink.kind == KIND_TYPED_LINK, or_(*ends)).limit(1)
    )
    if existing is not None:
        raise ValidationError(_("A typed link between these items already exists"), field="other_ref")
    stored_ref = target_ref.split("/", 1)[1] if target[0] == ITEM_WIKI_PAGE else target_ref
    link = EntityLink(
        source_type=source[0],
        source_issue_id=source[1] if source[0] == ITEM_ISSUE else None,
        source_wiki_page_id=source[1] if source[0] == ITEM_WIKI_PAGE else None,
        source_project_id=source_project_id,
        kind=KIND_TYPED_LINK,
        target_type=target[0],
        target_issue_id=target[1] if target[0] == ITEM_ISSUE else None,
        target_wiki_page_id=target[1] if target[0] == ITEM_WIKI_PAGE else None,
        target_project_id=target_project_id,
        target_ref=stored_ref[:255],
        link_type=link_type,
    )
    session.add(link)
    await session.flush()
    return link


def _source_column(item: Item) -> Any:
    return EntityLink.source_issue_id if item[0] == ITEM_ISSUE else EntityLink.source_wiki_page_id


def _target_column(item: Item) -> Any:
    return EntityLink.target_issue_id if item[0] == ITEM_ISSUE else EntityLink.target_wiki_page_id


# ---------------------------------------------------------------------------
# Queueing the computation
# ---------------------------------------------------------------------------


def dispatch_compute(*, project_id: int | None = None, items: Iterable[Item] | None = None) -> bool:
    """Queue ``compute_similar_links`` for a project or for *items*; never raises.

    Suggestions are hints: a dead broker only delays them until the nightly sweep.
    """
    payload = None if items is None else [[t, int(i)] for t, i in sorted(set(items))]
    if project_id is None and not payload:
        return False
    try:
        from specivo.tasks.link_suggestions import compute_similar_links

        compute_similar_links.apply_async(kwargs={"project_id": project_id, "items": payload}, retry=False)
    except Exception as exc:  # a dead broker must not fail the caller  # noqa: BLE001
        logger.warning("Could not queue the suggested links computation: %s", exc)
        return False
    return True
