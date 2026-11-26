"""Concept nodes of the link graph: the concepts extraction found, and the issues and pages that mention them.

A concept (``concept_extraction``) is a hub node, ref ``concept:KEY/<normalized
name>``, joined to each issue or wiki page that mentions it by an edge of kind
``mentions`` from the item to the concept. The edge's ``label`` is the model's
confidence (two decimals) and ``passage`` the excerpt it quoted. A mention
below the project's confidence threshold is a suggestion: ``suggested: true``,
and only read when suggestions are asked for, like the ``similar`` edges.

Visibility
----------
A concept is never shown because of its project alone: it is visible only to a
reader who may see at least one source that mentions it, so a concept found
only in a private issue or a page of an unreadable wiki stays hidden. Only
sources that are still in the concept's project count (an issue moved away
stops counting at once, before the next run forgets it), and nothing is shown
while the project has the extraction switched off. A concept's ``degree``
counts the mentions at or above the threshold in sources the reader may see.

The queries here are the concept half of ``entity_link_nodes.HubReader``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.concept import ConceptMention, EntityConcept
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.wiki import Wiki, WikiPage

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement

    from specivo.services.entity_link_nodes import HubReader
    from specivo.services.entity_link_service import LinkNode, NodeKey, _RawEdge

KIND_MENTIONS = "mentions"
NODE_CONCEPT = "concept"


def _enabled() -> ColumnElement[bool]:
    from specivo.services.concept_extraction import ENABLED_SETTING

    clause: ColumnElement[bool] = Project.settings[ENABLED_SETTING].as_boolean().is_(True)
    return clause


def _issue_join() -> Any:
    """Issue sources that are still in their concept's project."""
    return and_(Issue.id == ConceptMention.source_issue_id, Issue.project_id == EntityConcept.project_id)


def _page_join() -> Any:
    """Live wiki page sources of their concept's project's wiki."""
    return and_(WikiPage.id == ConceptMention.source_wiki_page_id, WikiPage.deleted_at.is_(None))


def _mentions(columns: Iterable[Any], *, pages: bool) -> Any:
    """``select(columns)`` over the mentions of enabled projects, joined to their issue or page source."""
    stmt = (
        select(*columns)
        .select_from(ConceptMention)
        .join(EntityConcept, EntityConcept.id == ConceptMention.concept_id)
        .join(Project, Project.id == EntityConcept.project_id)
    )
    if pages:
        return (
            stmt.join(WikiPage, _page_join())
            .join(Wiki, Wiki.id == WikiPage.wiki_id)
            .where(Wiki.project_id == EntityConcept.project_id, _enabled())
        )
    return stmt.join(Issue, _issue_join()).where(_enabled())


def _edge(item: NodeKey, concept_id: int, mention_id: int, confidence: float, passage: str, threshold: float) -> Any:
    from specivo.services.entity_link_service import _RawEdge

    return _RawEdge(
        item,
        (NODE_CONCEPT, concept_id),
        KIND_MENTIONS,
        label=f"{confidence:.2f}",
        identity=(KIND_MENTIONS, mention_id),
        suggested=confidence < threshold,
        passage=passage,
    )


def _threshold(settings: dict[str, Any] | None) -> float:
    from specivo.services.concept_extraction import confidence_threshold

    return confidence_threshold(settings)


_EDGE_COLUMNS = (
    ConceptMention.id,
    ConceptMention.concept_id,
    ConceptMention.confidence,
    ConceptMention.passage,
    Project.settings,
)


async def item_concept_edges(session: AsyncSession, node: NodeKey) -> list[_RawEdge]:
    """The ``mentions`` edges of one issue or page (any confidence; the reader drops suggestions not asked for)."""
    if node[0] == "issue":
        stmt = _mentions(_EDGE_COLUMNS, pages=False).where(ConceptMention.source_issue_id == node[1])
    elif node[0] == "wiki_page":
        stmt = _mentions(_EDGE_COLUMNS, pages=True).where(ConceptMention.source_wiki_page_id == node[1])
    else:
        return []
    rows = await session.execute(stmt.order_by(ConceptMention.confidence.desc(), ConceptMention.id))
    return [
        _edge(node, concept_id, mention_id, confidence, passage, _threshold(settings))
        for mention_id, concept_id, confidence, passage, settings in rows
    ]


async def concept_edges_of(
    session: AsyncSession, issues: set[int], pages: set[int], limit: int
) -> tuple[list[_RawEdge], bool]:
    """Confident ``mentions`` edges of many issues and pages (for path finding); and whether *limit* was hit."""
    edges: list[_RawEdge] = []
    hit = False
    columns = (*_EDGE_COLUMNS, ConceptMention.source_issue_id, ConceptMention.source_wiki_page_id)
    for ids, pages_side in ((issues, False), (pages, True)):
        if not ids:
            continue
        column = ConceptMention.source_wiki_page_id if pages_side else ConceptMention.source_issue_id
        rows = (
            await session.execute(
                _mentions(columns, pages=pages_side).where(column.in_(ids)).order_by(ConceptMention.id).limit(limit)
            )
        ).all()
        hit = hit or len(rows) >= limit
        for mention_id, concept_id, confidence, passage, settings, issue_id, page_id in rows:
            item: NodeKey = ("issue", issue_id) if issue_id is not None else ("wiki_page", page_id)
            edge = _edge(item, concept_id, mention_id, confidence, passage, _threshold(settings))
            if not edge.suggested:
                edges.append(edge)
    return edges, hit


# ---------------------------------------------------------------------------
# HubReader helpers
# ---------------------------------------------------------------------------


async def concept_projects(session: AsyncSession, ids: Iterable[int]) -> dict[int, int]:
    """``concept id -> project id`` for the concepts of projects with the extraction on."""
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    rows = await session.execute(
        select(EntityConcept.id, EntityConcept.project_id)
        .join(Project, Project.id == EntityConcept.project_id)
        .where(EntityConcept.id.in_(wanted), _enabled())
    )
    return {concept_id: project_id for concept_id, project_id in rows}


async def visible_concepts(hubs: HubReader, ids: Iterable[int], wiki_projects: set[int]) -> set[int]:
    """The concepts among *ids* that at least one source the reader may see mentions.

    *wiki_projects*: the projects whose wiki the reader may read.
    """
    wanted = sorted(set(ids))
    if not wanted:
        return set()
    clause = await hubs.issue_clause()
    by_issue = _mentions([ConceptMention.concept_id], pages=False).where(ConceptMention.concept_id.in_(wanted))
    if clause is not None:
        by_issue = by_issue.where(clause)
    visible = set((await hubs.session.execute(by_issue.distinct())).scalars())
    rest = [i for i in wanted if i not in visible]
    if rest and wiki_projects:
        by_page = _mentions([ConceptMention.concept_id], pages=True).where(
            ConceptMention.concept_id.in_(rest), EntityConcept.project_id.in_(sorted(wiki_projects))
        )
        visible |= set((await hubs.session.execute(by_page.distinct())).scalars())
    return visible


async def concept_member_edges(hubs: HubReader, concept: NodeKey, cap: int, *, wiki_readable: bool) -> list[_RawEdge]:
    """The visible issues and pages that mention *concept*, best confidence first, at most *cap* of each."""
    columns = (*_EDGE_COLUMNS, ConceptMention.source_issue_id, ConceptMention.source_wiki_page_id)
    clause = await hubs.issue_clause()
    by_issue = _mentions(columns, pages=False).where(ConceptMention.concept_id == concept[1])
    if clause is not None:
        by_issue = by_issue.where(clause)
    statements = [by_issue]
    if wiki_readable:
        statements.append(_mentions(columns, pages=True).where(ConceptMention.concept_id == concept[1]))
    edges: list[_RawEdge] = []
    for stmt in statements:
        rows = await hubs.session.execute(stmt.order_by(ConceptMention.confidence.desc(), ConceptMention.id).limit(cap))
        for mention_id, concept_id, confidence, passage, settings, issue_id, page_id in rows:
            item: NodeKey = ("issue", issue_id) if issue_id is not None else ("wiki_page", page_id)
            edges.append(_edge(item, concept_id, mention_id, confidence, passage, _threshold(settings)))
    return edges


async def concept_degrees(hubs: HubReader, ids: list[int], wiki_projects: set[int]) -> dict[int, int]:
    """Mentions at or above the threshold, in sources the reader may see, per concept."""
    counts: dict[int, int] = dict.fromkeys(ids, 0)
    if not ids:
        return counts
    clause = await hubs.issue_clause()
    columns = (ConceptMention.concept_id, ConceptMention.confidence, Project.settings)
    by_issue = _mentions(columns, pages=False).where(ConceptMention.concept_id.in_(ids))
    if clause is not None:
        by_issue = by_issue.where(clause)
    statements = [by_issue]
    if wiki_projects:
        statements.append(
            _mentions(columns, pages=True).where(
                ConceptMention.concept_id.in_(ids), EntityConcept.project_id.in_(sorted(wiki_projects))
            )
        )
    for stmt in statements:
        for concept_id, confidence, settings in await hubs.session.execute(stmt):
            if confidence >= _threshold(settings):
                counts[concept_id] += 1
    return counts


async def describe_concepts(session: AsyncSession, ids: list[int], keys: dict[int, str]) -> dict[NodeKey, LinkNode]:
    """``concept:KEY/<normalized>`` nodes: the name as title, the kind as status."""
    from specivo.services.entity_link_service import LinkNode

    out: dict[NodeKey, LinkNode] = {}
    if not ids:
        return out
    for concept_id, name, normalized, kind, project_id in await session.execute(
        select(
            EntityConcept.id, EntityConcept.name, EntityConcept.normalized, EntityConcept.kind, EntityConcept.project_id
        ).where(EntityConcept.id.in_(ids))
    ):
        key = keys.get(project_id)
        if key is None:
            continue
        out[(NODE_CONCEPT, concept_id)] = LinkNode(f"concept:{key}/{normalized}", NODE_CONCEPT, name, kind, None, key)
    return out


async def resolve_concept(session: AsyncSession, project: Project, name: str) -> int | None:
    """The id of the concept *name* (normalised the way extraction does) in *project*, if any."""
    from specivo.services.concept_extraction import normalize_name

    normalized = normalize_name(name)
    if normalized is None:
        return None
    return await session.scalar(
        select(EntityConcept.id).where(EntityConcept.project_id == project.id, EntityConcept.normalized == normalized)
    )
