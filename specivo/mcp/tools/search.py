"""MCP tool: keyword, semantic and hybrid search."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import MCP_SEARCH_MAX_LIMIT, SEARCH_SNIPPET_MAX_CHARS
from specivo.core.exceptions import (
    NotFoundError,
    PermissionDeniedError,
    UnauthorizedError,
    ValidationError,
)
from specivo.mcp.tools._common import _log_tool, _project_svc, _search_svc
from specivo.models.user import User
from specivo.services.entity_link_query import compact_edges, expand_hits
from specivo.services.entity_link_service import LinkGraph
from specivo.services.security_audit_service import AuditEvent

SEARCH_MODES = ("hybrid", "keyword", "semantic")


async def _search(
    session: AsyncSession,
    user: User,
    query: str,
    project_key: str | None = None,
    scope: str = "all",
    limit: int = 10,
    mode: str = "hybrid",
    expand: int = 0,
) -> str:
    if mode not in SEARCH_MODES:
        raise ValidationError(f"Invalid mode '{mode}'. Must be one of: {', '.join(SEARCH_MODES)}")
    # The tool has no offset; its limit is the only depth a caller controls.
    limit = max(1, min(limit, MCP_SEARCH_MAX_LIMIT))

    project_id: int | None = None
    if project_key:
        # A private project the caller is not a member of, or one outside the
        # API key's limit, is "not found" exactly like a missing key.
        try:
            project = await _project_svc.get_readable_by_key(session, project_key, user)
        except (PermissionDeniedError, UnauthorizedError):
            # Any other refusal reads as a missing key too, word for word.
            raise NotFoundError(f"Project '{project_key}' not found") from None
        project_id = project.id

    if mode == "hybrid":
        results, total, _type_counts = await _search_svc.hybrid_search(
            session, query, user=user, project_id=project_id, scope=scope, limit=limit
        )
    elif mode == "semantic":
        results, total = await _search_svc.semantic_search(
            session, query, user=user, project_id=project_id, limit=limit
        )
        if scope != "all":
            scope_map = {"issues": "issue", "wiki": "wiki", "comments": "comment", "attachments": "attachment"}
            target = scope_map.get(scope)
            if target:
                results = [r for r in results if r.result_type == target]
                total = len(results)
    else:
        results, total, _type_counts = await _search_svc.search(
            session, query, user=user, project_id=project_id, scope=scope, limit=limit
        )

    context = await expand_hits(session, user, results) if expand else {}
    lines = [f"Search results for '{query}' [mode={mode}] ({total} total):", ""]
    for index, r in enumerate(results):
        lines.append(f"  [{r.result_type}] {r.title}  —  {r.subtitle or ''}")
        if r.snippet:
            lines.append(f"    {r.snippet[:SEARCH_SNIPPET_MAX_CHARS]}")
        if index in context:
            lines.extend(_context_lines(context[index]))
    if not results:
        lines.append("  (no results)")
    await _log_tool(
        session,
        user,
        AuditEvent.SEARCH_QUERY,
        "search",
        {"query": query, "scope": scope, "mode": mode, "result_count": total, "expand": expand},
    )
    return "\n".join(lines)


def _context_lines(graph: LinkGraph) -> list[str]:
    """The links of one hit, indented under it: its ref, then one line per edge."""
    root = graph.root.ref if graph.root else "?"
    if not graph.edges:
        return [f"    links of {root}: none"]
    more = " (more left out)" if graph.truncated else ""
    return [f"    links of {root}{more}:", *(f"      {line}" for line in compact_edges(graph))]
