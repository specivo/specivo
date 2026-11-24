"""MCP tools: the link graph between issues, wiki pages, attachments and structured nodes (tags, versions, ...)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp.tools._common import _link_svc, _log_tool
from specivo.models.user import User
from specivo.services.security_audit_service import AuditEvent


def _link_item(node: Any) -> str:
    text = f"{node.ref} {node.title}".strip()
    if node.status:
        text += f" [{node.status}]"
    return text


def _capped(label: str, items: list[str]) -> list[str]:
    """``Label: a; b; ...`` capped at ``ENTITY_LINKS_INLINE_LIMIT`` items, or nothing when empty."""
    from specivo.core.constants import ENTITY_LINKS_INLINE_LIMIT

    if not items:
        return []
    shown = items[:ENTITY_LINKS_INLINE_LIMIT]
    line = f"{label}: " + "; ".join(shown)
    if len(items) > len(shown):
        line += f"; ... and {len(items) - len(shown)} more"
    return [line]


async def _link_lines(session: AsyncSession, user: User, root: tuple[str, int]) -> list[str]:
    """``Links to:``, ``Linked from:``, ``Related:``, ``Broken links:`` and ``Possibly related:`` lines.

    Text links, typed links and relations only (parent, children, commits and
    pull requests have lines of their own), filtered for *user* like
    ``specivo_get_links``. Tags, versions, sprints, metadata values and the
    assignee are never listed here: the issue and page output already shows
    them, and a hub would only repeat itself on every item. ``Possibly
    related:`` lists at most ``SIMILAR_LINKS_INLINE_LIMIT`` suggested links
    (similar content, not linked), each with its score.
    """
    from specivo.core.constants import ENTITY_LINKS_MAX_LIMIT
    from specivo.models.entity_link import KIND_TYPED_LINK
    from specivo.services.entity_link_service import KIND_RELATION, STORED_LINK_KINDS

    graph = await _link_svc.get_links(
        session,
        user,
        root,
        direction="both",
        kinds=STORED_LINK_KINDS | {KIND_RELATION},
        limit=ENTITY_LINKS_MAX_LIMIT,
    )
    nodes = {n.ref: n for n in graph.nodes}
    links_to: list[str] = []
    linked_from: list[str] = []
    related: list[str] = []
    broken: list[str] = []
    seen: set[tuple[str, str]] = set()
    for edge in graph.edges:
        if edge.broken:
            if edge.target not in broken:
                broken.append(edge.target)
            continue
        if edge.kind == KIND_RELATION:
            node = nodes.get(edge.target)
            if node is not None:
                related.append(f"{edge.relation_type} {_link_item(node)}")
            continue
        other = edge.target if edge.direction == "out" else edge.source
        if (edge.direction or "", other) in seen or other not in nodes:
            continue
        seen.add((edge.direction or "", other))
        item = _link_item(nodes[other])
        if edge.kind == KIND_TYPED_LINK and edge.label:
            item += f" ({edge.label})"
        if edge.via and edge.direction == "in":
            item += f" (comment {edge.via})"
        (links_to if edge.direction == "out" else linked_from).append(item)
    lines = _capped("Links to", links_to) + _capped("Linked from", linked_from)
    lines += _capped("Related", related) + _capped("Broken links", broken)
    lines += await _suggestion_lines(session, user, root)
    return lines


async def _suggestion_lines(session: AsyncSession, user: User, root: tuple[str, int]) -> list[str]:
    """``Possibly related (suggested, not linked):`` with the best few suggested links the reader may see."""
    from specivo.core.constants import SIMILAR_LINKS_INLINE_LIMIT
    from specivo.services.entity_link_service import KIND_SIMILAR

    graph = await _link_svc.get_links(
        session, user, root, direction="both", kinds=[KIND_SIMILAR], limit=SIMILAR_LINKS_INLINE_LIMIT
    )
    nodes = {n.ref: n for n in graph.nodes}
    items = [
        f"{_link_item(nodes[edge.target])} (score {edge.label})"
        for edge in graph.edges
        if edge.suggested and edge.target in nodes
    ]
    if not items:
        return []
    return ["Possibly related (suggested, not linked): " + "; ".join(items[:SIMILAR_LINKS_INLINE_LIMIT])]


GET_LINKS_FORMATS = ("json", "compact")


async def _get_links(  # noqa: PLR0913 - one parameter per tool argument
    session: AsyncSession,
    user: User,
    ref: str,
    direction: str = "both",
    depth: int = 1,
    types: list[str] | None = None,
    limit: int = 50,
    to: str | None = None,
    filter: dict[str, Any] | None = None,
    format: str = "json",
    include_suggestions: bool = False,
) -> str:
    import json

    from specivo.core.constants import ENTITY_LINKS_MAX_LIMIT
    from specivo.services.entity_link_query import (
        apply_filter,
        cap_nodes,
        compact_graph,
        compact_path,
        find_path,
        parse_filter,
    )
    from specivo.services.entity_link_service import AGENT_DEFAULT_KINDS, KIND_SIMILAR, resolve_root

    if format not in GET_LINKS_FORMATS:
        raise ValueError(f"format must be one of: {', '.join(GET_LINKS_FORMATS)}")
    flt = parse_filter(filter)
    root = await resolve_root(session, user, ref, hubs=True)
    event = AuditEvent.WIKI_READ if root[0] == "wiki_page" else AuditEvent.ISSUE_READ
    if to is not None:
        if flt:
            raise ValueError("filter cannot be combined with to=; a path is the shortest one, whatever it passes")
        goal = await resolve_root(session, user, to, hubs=True)
        path = await find_path(session, user, root, goal, types=types)
        await _log_tool(session, user, event, "get_links", {"ref": ref, "to": to})
        if format == "compact":
            return compact_path(path)
        return json.dumps(path.as_dict(), ensure_ascii=False, indent=1)
    kinds: list[str] | None = list(types) if types else None
    if include_suggestions:
        kinds = sorted(set(kinds or AGENT_DEFAULT_KINDS) | {KIND_SIMILAR})
    graph = await _link_svc.get_links(
        session,
        user,
        root,
        direction=direction,
        depth=depth,
        kinds=kinds,
        # A filter drops nodes after the read: read as many as allowed, then cut to the limit.
        limit=ENTITY_LINKS_MAX_LIMIT if flt else limit,
        default_kinds=AGENT_DEFAULT_KINDS,
        count_attachments=True,
    )
    if flt:
        cap_nodes(await apply_filter(session, user, graph, flt), limit)
    await _log_tool(session, user, event, "get_links", {"ref": ref, "direction": direction})
    if format == "compact":
        return compact_graph(graph)
    return json.dumps(graph.as_dict(), ensure_ascii=False, indent=1)


async def _link_suggestion(
    session: AsyncSession,
    user: User,
    op: str,
    ref: str,
    other_ref: str,
    link_type: str | None = None,
) -> str:
    """Accept or dismiss the suggested link between *ref* and *other_ref*, both of which *user* must see."""
    from specivo.core.exceptions import ValidationError
    from specivo.services.entity_link_service import resolve_root
    from specivo.services.link_suggestion_service import OP_ACCEPT, OPS, decide

    if op not in OPS:
        raise ValidationError(f"op must be one of: {', '.join(OPS)}", field="op")
    source = await resolve_root(session, user, ref)
    target = await resolve_root(session, user, other_ref)
    decision = await decide(session, user, op, source, target, link_type=link_type)
    event = AuditEvent.LINK_SUGGESTION_ACCEPTED if op == OP_ACCEPT else AuditEvent.LINK_SUGGESTION_DISMISSED
    details = {"op": op, "ref": decision.source_ref, "other_ref": decision.target_ref}
    if decision.link_type:
        details["link_type"] = decision.link_type
    await _log_tool(session, user, event, "link_suggestion", details, project_id=decision.project_id)
    return decision.describe()
