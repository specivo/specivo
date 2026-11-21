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
    """``Links to:``, ``Linked from:``, ``Related:`` and ``Broken links:`` lines for an issue or wiki page.

    Text links and relations only (parent, children, commits and pull requests
    have lines of their own), filtered for *user* like ``specivo_get_links``.
    Tags, versions, sprints, metadata values and the assignee are never listed
    here: the issue and page output already shows them, and a hub would only
    repeat itself on every item.
    """
    from specivo.core.constants import ENTITY_LINKS_MAX_LIMIT
    from specivo.services.entity_link_service import KIND_RELATION, TEXT_LINK_KINDS

    graph = await _link_svc.get_links(
        session,
        user,
        root,
        direction="both",
        kinds=TEXT_LINK_KINDS | {KIND_RELATION},
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
        if edge.via and edge.direction == "in":
            item += f" (comment {edge.via})"
        (links_to if edge.direction == "out" else linked_from).append(item)
    lines = _capped("Links to", links_to) + _capped("Linked from", linked_from)
    lines += _capped("Related", related) + _capped("Broken links", broken)
    return lines


async def _get_links(
    session: AsyncSession,
    user: User,
    ref: str,
    direction: str = "both",
    depth: int = 1,
    types: list[str] | None = None,
    limit: int = 50,
) -> str:
    import json

    from specivo.services.entity_link_service import AGENT_DEFAULT_KINDS, resolve_root

    root = await resolve_root(session, user, ref, hubs=True)
    graph = await _link_svc.get_links(
        session,
        user,
        root,
        direction=direction,
        depth=depth,
        kinds=types or None,
        limit=limit,
        default_kinds=AGENT_DEFAULT_KINDS,
        count_attachments=True,
    )
    event = AuditEvent.WIKI_READ if root[0] == "wiki_page" else AuditEvent.ISSUE_READ
    await _log_tool(session, user, event, "get_links", {"ref": ref, "direction": direction})
    return json.dumps(graph.as_dict(), ensure_ascii=False, indent=1)
