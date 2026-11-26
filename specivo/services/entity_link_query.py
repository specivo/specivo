"""Queries over the link graph: path finding, filters on node properties, compact text and search context.

Built on ``entity_link_service`` (the reads and the reader's visibility) and
``entity_link_nodes`` (hubs). Nothing here is stored.

Path finding (:func:`find_path`)
--------------------------------
The shortest path between two nodes the reader may see, as a bidirectional
breadth-first search over visible edges only: at most
``ENTITY_LINKS_PATH_MAX_HOPS`` hops, and it gives up once it has discovered
``ENTITY_LINKS_PATH_NODE_BUDGET`` nodes. Every edge is filtered for the
reader before its far end is discovered, so a path never runs through
something the reader cannot see.

The budget also holds inside a hop: each hop reads its edges in a fixed
number of batched queries over the whole frontier, each limited to the nodes
still allowed, plus one member query for each of at most
``ENTITY_LINKS_PATH_MAX_HUBS`` hubs; discovery stops once the budget is spent.
When a capped read may have left a path out, the answer is
``budget_exhausted``, never "no path".

- It steps through issues and wiki pages over text links, relations and
  parent/child (or over the kinds ``types`` names). Attachments, commits and
  pull requests are dead ends and are never followed.
- Hubs (tag, component, metadata value, version, sprint, user) join so much
  that they would make most paths trivial: a hub is stepped through only when
  ``types`` names its edge kind. A hub may always be either end.
- Edges are followed in both directions; the path lists each edge as it is
  stored (``source``, ``target``, ``kind``).

Filters (:func:`apply_filter`)
------------------------------
``{"status": "open", "component": "importer"}`` keeps the issues whose
properties match every key (any of the values when a key has a list), after
the traversal:

- ``status``: ``open`` (not in a closed status), ``closed``, or a status name;
  ``tracker``: a tracker name; ``version``: the target version's name;
  ``assignee``: a login, or ``me``; any other key is an issue metadata key the
  issue's project lists in ``graph_metadata_keys`` (``component`` by default).
  Names compare without regard to case.
- Every key is an issue property, so a filter drops the wiki pages. The root is
  always kept, whatever it is.
- Other nodes (tags, versions, users, commits, pull requests, attachments)
  stay while an edge still joins them to the root or a kept issue; edges to a
  dropped node, and links to missing targets, are dropped.

Compact text (:func:`compact_graph`, :func:`compact_path`)
---------------------------------------------------------
One line per edge, ``SRC -kind-> DST "title"``, refs as a user types them.
A node's title (and status, degree, files) is written the first time the node
appears, right after its ref, so ``ACME-3 "Login fails" [Open] -relation:blocks->
ACME-1`` reads the same as an outgoing edge. The first line names the root and
says when the read was truncated.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import (
    ENTITY_LINKS_PATH_MAX_HOPS,
    ENTITY_LINKS_PATH_MAX_HUBS,
    ENTITY_LINKS_PATH_NODE_BUDGET,
    SEARCH_EXPAND_HITS,
    SEARCH_EXPAND_NEIGHBOURS,
)
from specivo.models.entity_link import EntityLink
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.lookups import IssueStatus, StatusCategory, Tracker
from specivo.models.project import Project
from specivo.models.relation import IssueRelation
from specivo.models.user import User
from specivo.models.version import Version
from specivo.models.wiki import WikiPage
from specivo.services.entity_link_nodes import (
    COMPONENT_KEY,
    HUB_KIND_OF,
    HUB_KINDS,
    HUB_TYPES,
    graph_metadata_keys,
    hub_edges_of,
    metadata_values,
)
from specivo.services.entity_link_service import (
    AGENT_DEFAULT_KINDS,
    ALL_KINDS,
    KIND_CHILD,
    KIND_PARENT,
    KIND_RELATION,
    STORED_LINK_KINDS,
    EntityLinkService,
    LinkEdge,
    LinkGraph,
    LinkNode,
    NodeKey,
    _hierarchy_edge,
    _RawEdge,
    _Reader,
    _target_node,
    link_label,
)

# What a path follows when ``types`` is not given: links between items, no hubs.
PATH_KINDS = STORED_LINK_KINDS | {KIND_RELATION, KIND_PARENT, KIND_CHILD}
NO_PATH = "no_path_within_4_hops"
BUDGET_EXHAUSTED = "budget_exhausted"
_ITEM_TYPES = ("issue", "wiki_page")

# ---------------------------------------------------------------------------
# Path finding
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PathResult:
    """The shortest path from ``root`` to ``to``, or ``reason`` when there is none to report."""

    root: LinkNode | None
    to: LinkNode | None
    nodes: list[LinkNode] | None = None
    edges: list[LinkEdge] | None = None
    reason: str | None = None
    explored: int = 0

    def as_dict(self) -> dict[str, Any]:
        from specivo.services.entity_link_service import _edge_dict, _node_dict

        path = None
        if self.nodes is not None and self.edges is not None:
            path = {
                "hops": len(self.edges),
                "nodes": [_node_dict(n) for n in self.nodes],
                "edges": [_edge_dict(e) for e in self.edges],
            }
        return {
            "root": _node_dict(self.root) if self.root else None,
            "to": _node_dict(self.to) if self.to else None,
            "path": path,
            "reason": self.reason,
            "explored": self.explored,
        }


def path_kinds(types: Iterable[str] | None) -> tuple[frozenset[str], frozenset[str]]:
    """The kinds a path follows for *types*, and the hub kinds among them (hubs it may step through)."""
    explicit = frozenset(types or ())
    unknown = explicit - ALL_KINDS
    if unknown:
        raise ValueError(f"Unknown link kind(s): {', '.join(sorted(unknown))}")
    if not explicit:
        return PATH_KINDS, frozenset()
    walk = explicit & (PATH_KINDS | HUB_KINDS)
    if not walk:
        raise ValueError(
            "A path follows only text links, relations, parent/child and hub kinds; "
            f"types names none of them: {', '.join(sorted(explicit))}"
        )
    return walk, walk & HUB_KINDS


@dataclass(slots=True)
class _Side:
    """One end of the bidirectional search: how each discovered node was reached, and its distance."""

    start: NodeKey
    parent: dict[NodeKey, tuple[NodeKey, _RawEdge] | None] = field(default_factory=dict)
    depth: dict[NodeKey, int] = field(default_factory=dict)
    frontier: list[NodeKey] = field(default_factory=list)
    level: int = 0

    def __post_init__(self) -> None:
        self.parent[self.start] = None
        self.depth[self.start] = 0
        self.frontier = [self.start]

    def chain(self, node: NodeKey) -> list[tuple[NodeKey, _RawEdge]]:
        """The (node, edge) steps from *node* back to this side's start."""
        steps: list[tuple[NodeKey, _RawEdge]] = []
        step = self.parent.get(node)
        while step is not None:
            steps.append(step)
            step = self.parent.get(step[0])
        return steps


class _PathSearch:
    def __init__(
        self,
        session: AsyncSession,
        reader: _Reader,
        start: NodeKey,
        goal: NodeKey,
        kinds: frozenset[str],
        hub_kinds: frozenset[str],
    ) -> None:
        self.session = session
        self.reader = reader
        self.kinds = kinds
        self.hub_kinds = hub_kinds
        self.forward = _Side(start)
        self.backward = _Side(goal)
        # Edges to hubs are read for the hubs it steps through, and for a hub at either end, so the
        # two searches meet there whichever side reaches it first; the other hubs stay dead ends.
        self.read_hub_kinds = hub_kinds | {HUB_KIND_OF[n[0]] for n in (start, goal) if n[0] in HUB_TYPES}
        # Something was left out (a capped query, hub or frontier): a miss is then no proof of "no path".
        self.partial = False
        # The node budget is spent.
        self.exhausted = False

    @property
    def explored(self) -> int:
        return len(self.forward.parent.keys() | self.backward.parent.keys())

    async def run(self) -> tuple[NodeKey | None, str | None]:
        """Return the node where the two searches meet on a shortest path, or None and the reason."""
        if self.forward.start == self.backward.start:
            return self.forward.start, None
        while self.forward.frontier and self.backward.frontier and not self.exhausted:
            if self.forward.level + self.backward.level >= ENTITY_LINKS_PATH_MAX_HOPS:
                break
            side, other = (
                (self.forward, self.backward)
                if len(self.forward.frontier) <= len(self.backward.frontier)
                else (self.backward, self.forward)
            )
            meet = await self._expand(side, other)
            if meet is not None:
                return meet, None
        if self.exhausted or self.partial:
            return None, BUDGET_EXHAUSTED
        return None, NO_PATH

    @property
    def remaining(self) -> int:
        """Nodes that may still be discovered."""
        return ENTITY_LINKS_PATH_NODE_BUDGET - self.explored

    async def _expand(self, side: _Side, other: _Side) -> NodeKey | None:
        """Read one more hop around *side*'s frontier; return the best meeting node it found, if any.

        The budget holds inside the hop too: every query reads at most the
        nodes still allowed, and discovery stops once they are spent.
        """
        budget = self.remaining
        if budget <= 0:
            self.exhausted = True
            return None
        frontier_list = side.frontier[:budget]
        self.partial = self.partial or len(frontier_list) < len(side.frontier)
        frontier = set(frontier_list)
        edges = await self.reader.filter(await self._edges(frontier_list, budget))
        side.level += 1
        found: list[NodeKey] = []
        meets: list[tuple[int, NodeKey]] = []
        for edge in edges:
            if edge.source is None or edge.target is None:
                continue
            for near, far in ((edge.source, edge.target), (edge.target, edge.source)):
                if near not in frontier or far in side.parent:
                    continue
                if len(found) >= budget:
                    self.exhausted = True
                    break
                side.parent[far] = (near, edge)
                side.depth[far] = side.level
                found.append(far)
                if far in other.depth:
                    meets.append((side.level + other.depth[far], far))
        side.frontier = [n for n in found if self._steps_through(n)]
        return min(meets, key=lambda meet: meet[0])[1] if meets else None

    def _steps_through(self, node: NodeKey) -> bool:
        if node[0] in _ITEM_TYPES:
            return True
        return node[0] in HUB_TYPES and HUB_KIND_OF[node[0]] in self.hub_kinds

    async def _edges(self, frontier: Sequence[NodeKey], budget: int) -> list[_RawEdge]:
        """The edges around *frontier*, in a fixed number of queries plus one per hub (at most a few hubs)."""
        cap = _RowCap(budget + 1)
        issues = {n[1] for n in frontier if n[0] == "issue"}
        pages = {n[1] for n in frontier if n[0] == "wiki_page"}
        edges = await item_edges(self.session, issues, pages, self.kinds, cap)
        if self.read_hub_kinds:
            hub_edges, hit = await hub_edges_of(self.session, issues, pages, self.read_hub_kinds, cap.limit)
            cap.hit = cap.hit or hit
            edges.extend(hub_edges)
        edges.extend(await self._member_edges([n for n in frontier if n[0] in HUB_TYPES], budget))
        self.partial = self.partial or cap.hit
        # A path runs over links, never over suggestions (low-confidence concept mentions).
        return [e for e in edges if not e.suggested]

    async def _member_edges(self, hubs: list[NodeKey], budget: int) -> list[_RawEdge]:
        """The members of the hubs in the frontier, while the budget lasts; at most a few hubs per hop."""
        if len(hubs) > ENTITY_LINKS_PATH_MAX_HUBS:
            hubs = hubs[:ENTITY_LINKS_PATH_MAX_HUBS]
            self.partial = True
        edges: list[_RawEdge] = []
        for hub in hubs:
            left = budget - len(edges)
            if left <= 0:
                self.partial = True
                break
            members = await self.reader.hubs.member_edges(hub, frozenset({HUB_KIND_OF[hub[0]]}), left + 1)
            if len(members) > left:
                self.partial = True
                members = members[:left]
            edges.extend(members)
        return edges


@dataclass(slots=True)
class _RowCap:
    """At most ``limit`` rows per query; ``hit`` records that a query reached it (something was left out)."""

    limit: int
    hit: bool = False

    def check(self, rows: Sequence[Any]) -> Sequence[Any]:
        self.hit = self.hit or len(rows) >= self.limit
        return rows


async def item_edges(
    session: AsyncSession, issues: set[int], pages: set[int], kinds: frozenset[str], cap: _RowCap
) -> list[_RawEdge]:
    """Text links, relations and parent/child edges of many issues and pages at once, in either direction.

    One query per edge family, each with at most ``cap.limit`` rows. Links to
    missing targets and to attachments are left out: a path cannot run
    through them.
    """
    edges: list[_RawEdge] = []
    if kinds & STORED_LINK_KINDS and (issues or pages):
        edges.extend(await _text_edges(session, issues, pages, kinds & STORED_LINK_KINDS, cap))
    if issues and KIND_RELATION in kinds:
        edges.extend(await _relations_of(session, issues, cap))
    if kinds & {KIND_PARENT, KIND_CHILD}:
        from_child = KIND_PARENT in kinds
        edges.extend(await _hierarchy_of(session, "issue", issues, cap, from_child=from_child))
        edges.extend(await _hierarchy_of(session, "wiki_page", pages, cap, from_child=from_child))
    return edges


async def _relations_of(session: AsyncSession, issues: set[int], cap: _RowCap) -> list[_RawEdge]:
    stmt = (
        select(IssueRelation)
        .where(or_(IssueRelation.issue_from_id.in_(issues), IssueRelation.issue_to_id.in_(issues)))
        .order_by(IssueRelation.id)
        .limit(cap.limit)
    )
    rows = cap.check((await session.execute(stmt)).scalars().all())
    return [
        _RawEdge(
            ("issue", rel.issue_from_id),
            ("issue", rel.issue_to_id),
            KIND_RELATION,
            relation_type=rel.relation_type,
            identity=(KIND_RELATION, rel.id),
        )
        for rel in rows
    ]


async def _hierarchy_of(
    session: AsyncSession, node_type: str, ids: set[int], cap: _RowCap, *, from_child: bool
) -> list[_RawEdge]:
    """The parent and the children of each issue (or each page) in *ids*."""
    if not ids:
        return []
    model: type[Issue] | type[WikiPage] = Issue if node_type == "issue" else WikiPage
    stmt = (
        select(model.id, model.parent_id)
        .where(or_(model.id.in_(ids) & model.parent_id.is_not(None), model.parent_id.in_(ids)))
        .order_by(model.id)
        .limit(cap.limit)
    )
    rows = cap.check((await session.execute(stmt)).all())
    return [
        _hierarchy_edge(node_type, child_id, parent_id, from_child=from_child)
        for child_id, parent_id in rows
        if parent_id is not None
    ]


async def _text_edges(
    session: AsyncSession, issues: set[int], pages: set[int], kinds: frozenset[str], cap: _RowCap
) -> list[_RawEdge]:
    conditions = []
    if issues:
        conditions += [
            EntityLink.source_issue_id.in_(issues),
            Journal.issue_id.in_(issues),
            EntityLink.target_issue_id.in_(issues),
        ]
    if pages:
        conditions += [
            EntityLink.source_wiki_page_id.in_(pages),
            Journal.wiki_page_id.in_(pages),
            EntityLink.target_wiki_page_id.in_(pages),
        ]
    stmt = (
        select(EntityLink, Journal.issue_id, Journal.wiki_page_id)
        .outerjoin(Journal, Journal.id == EntityLink.source_journal_id)
        .where(EntityLink.kind.in_(kinds), or_(*conditions))
        .order_by(EntityLink.id)
        .limit(cap.limit)
    )
    rows = cap.check((await session.execute(stmt)).all())
    edges: list[_RawEdge] = []
    for link, journal_issue_id, journal_page_id in rows:
        source = _link_source(link, journal_issue_id, journal_page_id)
        target = _target_node(link)
        if source is None or target is None or target[0] not in _ITEM_TYPES or source == target:
            continue
        edges.append(
            _RawEdge(
                source,
                target,
                link.kind,
                label=link_label(link),
                via_journal_id=link.source_journal_id,
                identity=("text", link.id),
            )
        )
    return edges


def _link_source(link: EntityLink, journal_issue_id: int | None, journal_page_id: int | None) -> NodeKey | None:
    """The issue or page whose text (or comment) holds *link*."""
    if link.source_issue_id is not None:
        return ("issue", link.source_issue_id)
    if link.source_wiki_page_id is not None:
        return ("wiki_page", link.source_wiki_page_id)
    if journal_issue_id is not None:
        return ("issue", journal_issue_id)
    if journal_page_id is not None:
        return ("wiki_page", journal_page_id)
    return None


async def find_path(
    session: AsyncSession, user: User, start: NodeKey, goal: NodeKey, *, types: Iterable[str] | None = None
) -> PathResult:
    """The shortest path from *start* to *goal* over edges *user* may see (both already checked visible)."""
    kinds, hub_kinds = path_kinds(types)
    reader = _Reader(session, user)
    await reader.hubs.load([start, goal])
    search = _PathSearch(session, reader, start, goal, kinds, hub_kinds)
    meet, reason = await search.run()
    graph = LinkGraph()
    if meet is None:
        await reader.describe(graph, start, [start, goal])
        return PathResult(graph.root, _node_for(graph, goal), reason=reason, explored=search.explored)
    forward = search.forward.chain(meet)  # meet -> ... -> start
    backward = search.backward.chain(meet)  # meet -> ... -> goal
    nodes = [step[0] for step in reversed(forward)] + [meet] + [step[0] for step in backward]
    edges = [step[1] for step in reversed(forward)] + [step[1] for step in backward]
    graph.raw_edges = [(edge, False) for edge in edges]
    await reader.describe(graph, start, nodes)
    described = [n for n in (_node_for(graph, key) for key in nodes) if n is not None]
    return PathResult(
        graph.root,
        _node_for(graph, goal),
        nodes=described,
        edges=graph.edges,
        explored=search.explored,
    )


def _node_for(graph: LinkGraph, key: NodeKey) -> LinkNode | None:
    by_key = {k: ref for ref, k in graph.keys.items()}
    ref = by_key.get(key)
    if ref is None:
        return None
    if graph.root is not None and graph.root.ref == ref:
        return graph.root
    return next((n for n in graph.nodes if n.ref == ref), None)


# ---------------------------------------------------------------------------
# Filters on node properties
# ---------------------------------------------------------------------------

FILTER_STATUS = "status"
FILTER_TRACKER = "tracker"
FILTER_VERSION = "version"
FILTER_ASSIGNEE = "assignee"
FILTER_FIXED_KEYS = (FILTER_STATUS, FILTER_TRACKER, FILTER_VERSION, FILTER_ASSIGNEE)
_FILTER_MAX_KEYS = 10
_FILTER_MAX_VALUES = 20


def parse_filter(raw: Mapping[str, Any] | None) -> dict[str, tuple[str, ...]]:
    """``{key: value or [values]}`` as ``{key: (lower-cased values)}``; raise ValueError when malformed."""
    if not raw:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError('filter is an object such as {"status": "open", "component": "importer"}')
    if len(raw) > _FILTER_MAX_KEYS:
        raise ValueError(f"filter takes at most {_FILTER_MAX_KEYS} keys")
    parsed: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or not values or not all(isinstance(v, str) and v.strip() for v in values):
            raise ValueError(f"filter[{key!r}] is a non-empty string or a list of them")
        if len(values) > _FILTER_MAX_VALUES:
            raise ValueError(f"filter[{key!r}] takes at most {_FILTER_MAX_VALUES} values")
        parsed[str(key).strip()] = tuple(v.strip().lower() for v in values)
    return parsed


@dataclass(slots=True)
class _IssueFacts:
    status: str
    closed: bool
    tracker: str
    version: str | None
    assignee: str | None
    metadata: dict[str, Any]
    graph_keys: tuple[str, ...]


async def _issue_facts(session: AsyncSession, ids: set[int]) -> dict[int, _IssueFacts]:
    """The filterable properties of the issues *ids*, in one query."""
    if not ids:
        return {}
    rows = await session.execute(
        select(
            Issue.id,
            IssueStatus.name,
            IssueStatus.category,
            Tracker.name,
            Version.name,
            User.login,
            Issue.issue_metadata,
            Project.settings,
        )
        .join(IssueStatus, IssueStatus.id == Issue.status_id)
        .join(Tracker, Tracker.id == Issue.tracker_id)
        .join(Project, Project.id == Issue.project_id)
        .outerjoin(Version, Version.id == Issue.fixed_version_id)
        .outerjoin(User, User.id == Issue.assigned_to_id)
        .where(Issue.id.in_(ids))
    )
    return {
        issue_id: _IssueFacts(
            status=status.lower(),
            closed=category == StatusCategory.closed,
            tracker=tracker.lower(),
            version=version.lower() if version else None,
            assignee=login.lower() if login else None,
            metadata=metadata or {},
            graph_keys=graph_metadata_keys(settings),
        )
        for issue_id, status, category, tracker, version, login, metadata, settings in rows
    }


def _status_ok(facts: _IssueFacts, values: tuple[str, ...]) -> bool:
    return any(
        (v == "open" and not facts.closed) or (v == "closed" and facts.closed) or v == facts.status for v in values
    )


def _key_ok(facts: _IssueFacts, key: str, values: tuple[str, ...], me: str) -> bool:
    """Whether one issue matches one filter key (values are lower-cased already)."""
    if key == FILTER_STATUS:
        return _status_ok(facts, values)
    if key == FILTER_TRACKER:
        return facts.tracker in values
    if key == FILTER_VERSION:
        return facts.version in values
    if key == FILTER_ASSIGNEE:
        return facts.assignee in {me if v == "me" else v for v in values}
    if key not in facts.graph_keys:
        return False
    return any(v.lower() in values for v in metadata_values(facts.metadata.get(key)))


def _matches(facts: _IssueFacts, flt: Mapping[str, tuple[str, ...]], me: str) -> bool:
    return all(_key_ok(facts, key, values, me) for key, values in flt.items())


def _check_filter_keys(flt: Mapping[str, tuple[str, ...]], facts: Iterable[_IssueFacts]) -> None:
    known = {*FILTER_FIXED_KEYS, COMPONENT_KEY}
    for fact in facts:
        known.update(fact.graph_keys)
    unknown = sorted(set(flt) - known)
    if unknown:
        raise ValueError(
            f"Unknown filter key(s): {', '.join(unknown)}. Use status, tracker, version, assignee, or a metadata "
            "key the project lists in graph_metadata_keys (component by default)"
        )


async def apply_filter(
    session: AsyncSession, user: User, graph: LinkGraph, flt: Mapping[str, tuple[str, ...]]
) -> LinkGraph:
    """Keep the root, the issues matching *flt* and what still joins them; see the module docstring."""
    if not flt:
        return graph
    root_ref = graph.root.ref if graph.root else None
    issue_ids = {key[1]: ref for ref, key in graph.keys.items() if key[0] == "issue" and ref != root_ref}
    facts = await _issue_facts(session, set(issue_ids))
    _check_filter_keys(flt, facts.values())
    me = (user.login or "").lower()
    matched = {issue_ids[i] for i, fact in facts.items() if _matches(fact, flt, me)}
    kept = _with_joined_nodes(graph, {root_ref} | matched if root_ref else matched)
    graph.nodes = [n for n in graph.nodes if n.ref in kept]
    graph.edges = [e for e in graph.edges if not e.broken and e.source in kept and e.target in kept]
    return graph


def _with_joined_nodes(graph: LinkGraph, kept: set[str]) -> set[str]:
    """*kept* plus the nodes that are neither issues nor pages and have an edge to one of *kept*."""
    items = {ref for ref, key in graph.keys.items() if key[0] in _ITEM_TYPES}
    joined = set(kept)
    for edge in graph.edges:
        if edge.broken:
            continue
        if edge.source in kept and edge.target not in items:
            joined.add(edge.target)
        if edge.target in kept and edge.source not in items:
            joined.add(edge.source)
    return joined


def cap_nodes(graph: LinkGraph, limit: int) -> LinkGraph:
    """Keep the first *limit* neighbour nodes, and the edges between what is left; mark the graph truncated."""
    if len(graph.nodes) <= limit:
        return graph
    graph.nodes = graph.nodes[:limit]
    kept = {n.ref for n in graph.nodes} | ({graph.root.ref} if graph.root else set())
    graph.edges = [e for e in graph.edges if e.source in kept and (e.broken or e.target in kept)]
    graph.truncated = True
    return graph


# ---------------------------------------------------------------------------
# Compact text
# ---------------------------------------------------------------------------


def _label(node: LinkNode | None, ref: str) -> str:
    if node is None:
        return ref
    text = ref
    if node.title and node.title != ref and not ref.endswith("/" + node.title):
        text += ' "' + node.title.replace('"', "'") + '"'
    if node.status:
        text += f" [{node.status}]"
    if node.degree is not None:
        text += f" (degree {node.degree})"
    if node.attachments:
        text += f" (files {node.attachments})"
    return text


class _Writer:
    """Writes refs, adding a node's title the first time it is named."""

    def __init__(self, nodes: Iterable[LinkNode], shown: Iterable[str] = ()) -> None:
        self.nodes = {n.ref: n for n in nodes}
        self.shown = set(shown)

    def node(self, ref: str) -> str:
        if ref in self.shown:
            return ref
        self.shown.add(ref)
        return _label(self.nodes.get(ref), ref)

    def edge(self, edge: LinkEdge) -> str:
        kind = f"{edge.kind}:{edge.relation_type}" if edge.relation_type else edge.kind
        if edge.suggested or edge.kind in ("typed_link", "mentions"):
            # The score of a suggestion, the type of a typed link, the confidence of a mention.
            kind = f"{edge.kind}:{edge.label}"
        source = self.node(edge.source)
        target = f"{edge.target} (broken)" if edge.broken else self.node(edge.target)
        line = f"{source} -{kind}-> {target}"
        if edge.via:
            line += f" (in {edge.via})"
        if edge.suggested:
            line += " (suggested)"
        return line


def compact_edges(graph: LinkGraph) -> list[str]:
    """One line per edge of *graph*; the root counts as already shown."""
    writer = _Writer(graph.nodes, [graph.root.ref] if graph.root else [])
    lines = [writer.edge(e) for e in graph.edges]
    lines += [_label(n, n.ref) for n in graph.nodes if n.ref not in writer.shown]
    return lines


def compact_graph(graph: LinkGraph) -> str:
    """The header line (root, counts, truncated) and one line per edge."""
    root = _label(graph.root, graph.root.ref) if graph.root else "(none)"
    head = f"root: {root} ({len(graph.nodes)} nodes, {len(graph.edges)} edges"
    head += ", truncated)" if graph.truncated else ")"
    return "\n".join([head, *compact_edges(graph)])


def compact_path(result: PathResult) -> str:
    """``path: A -> B, N hops`` and the edges in order, or ``path: none (reason)``."""
    start = result.root.ref if result.root else "?"
    goal = result.to.ref if result.to else "?"
    if result.edges is None or result.nodes is None:
        return f"path: {start} -> {goal}: none ({result.reason}, {result.explored} nodes explored)"
    writer = _Writer(result.nodes)
    head = f"path: {start} -> {goal}, {len(result.edges)} hops"
    if not result.edges:
        return "\n".join([head, writer.node(start)])
    return "\n".join([head, *(writer.edge(e) for e in result.edges)])


# ---------------------------------------------------------------------------
# Search with one-hop context
# ---------------------------------------------------------------------------

_SEARCH_NODE_TYPES = {"issue": "issue", "wiki": "wiki_page"}


async def expand_hits(session: AsyncSession, user: User, hits: Sequence[Any]) -> dict[int, LinkGraph]:
    """The one-hop neighbours of the first ``SEARCH_EXPAND_HITS`` issue and wiki-page *hits*, by hit index.

    *hits* are search results (``result_type``, ``id``) the user may see.
    Each is read over ``AGENT_DEFAULT_KINDS`` (no attachments; hubs are
    leaves), at most ``SEARCH_EXPAND_NEIGHBOURS`` neighbours each, with one
    reader shared by all of them so visibility is decided in batches.
    """
    service = EntityLinkService()
    reader = _Reader(session, user)
    out: dict[int, LinkGraph] = {}
    seen: set[NodeKey] = set()
    for index, hit in enumerate(hits):
        node_type = _SEARCH_NODE_TYPES.get(hit.result_type)
        if node_type is None or (node_type, hit.id) in seen:
            continue
        root: NodeKey = (node_type, hit.id)
        seen.add(root)
        out[index] = await service.get_links(
            session,
            user,
            root,
            direction="both",
            depth=1,
            limit=SEARCH_EXPAND_NEIGHBOURS,
            default_kinds=AGENT_DEFAULT_KINDS,
            reader=reader,
        )
        if len(out) >= SEARCH_EXPAND_HITS:
            break
    return out
