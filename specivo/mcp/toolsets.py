"""MCP toolsets: which tools a connection lists and may call.

A client without a tool search of its own puts every listed tool into the
model's prompt on every turn, so a connection lists only the toolsets it
asks for. ``core`` is what an everyday agent working on issues and the wiki
needs; the others are opt-in:

- ``core``: identity, projects, members and lookups; issues, comments,
  relations, wiki, search and the link graph; ``specivo_metadata``,
  ``specivo_tag`` and the read-only lists their arguments point to (tags,
  metadata schemas, versions, sprints); time logging and the timer; the agent
  session; the setup guide.
- ``planning``: managing versions and sprints (create, update, start,
  complete, delete) and listing their issues.
- ``recurring``: recurring task patterns and their occurrences.
- ``admin``: metadata schema and tag management (create, update, delete).
- ``ai-spend``: AI usage logging and reports, AI subscriptions, billing
  accounts, reimbursement claims and spend budgets.

The connection chooses with the ``toolsets`` query parameter of the MCP URL:
``/mcp/?toolsets=core,ai-spend`` (or ``all``). Without it the connection gets
``DEFAULT_TOOLSETS``. ``ToolsetMiddleware`` reads it on every request: a
Streamable HTTP client sends the same URL each time, and a legacy SSE stream
opened with ``/mcp/sse/?toolsets=...`` announces a message endpoint that
carries the parameter too (``specivo.mcp.mount``).

The selection is static per connection: ``tools/list`` is filtered, and a
``tools/call`` of a tool outside the selection answers a tool error naming
the toolset to add. ``notifications/tools/list_changed`` is never sent.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolResult, TextContent

if TYPE_CHECKING:
    from mcp.server.context import CallNext, HandlerResult, ServerRequestContext

CORE = "core"
PLANNING = "planning"
RECURRING = "recurring"
ADMIN = "admin"
AI_SPEND = "ai-spend"
#: Selects every toolset.
ALL = "all"

#: The query parameter of the MCP URL that selects toolsets.
TOOLSETS_PARAM = "toolsets"

#: Every tool, in exactly one toolset. ``tests/unit/test_mcp_toolsets.py`` fails
#: when a registered tool is missing here or listed twice.
TOOLSETS: dict[str, tuple[str, ...]] = {
    CORE: (
        "specivo_setup_guide",
        "specivo_whoami",
        "specivo_list_projects",
        "specivo_list_members",
        "specivo_list_lookups",
        # Issues
        "specivo_list_issues",
        "specivo_show_issue",
        "specivo_issue_development",
        "specivo_create_issue",
        "specivo_update_issue",
        "specivo_move_issue",
        "specivo_edit_description",
        "specivo_add_comment",
        "specivo_list_comments",
        "specivo_list_relations",
        "specivo_add_relation",
        "specivo_remove_relation",
        # Wiki
        "specivo_list_wiki_pages",
        "specivo_read_wiki",
        "specivo_read_wiki_section",
        "specivo_create_wiki",
        "specivo_edit_wiki",
        "specivo_append_wiki",
        "specivo_replace_wiki_section",
        "specivo_update_wiki_metadata",
        "specivo_delete_wiki",
        "specivo_restore_wiki",
        # Search and the link graph
        "specivo_search",
        "specivo_get_links",
        "specivo_link_suggestion",
        # Metadata and tags on issues and pages, and the lists their arguments name
        "specivo_metadata",
        "specivo_list_metadata_schemas",
        "specivo_tag",
        "specivo_list_tags",
        # Lookups for the fixed_version_id and sprint_id of issue writes
        "specivo_list_versions",
        "specivo_list_sprints",
        # Time and the agent session
        "specivo_log_time",
        "specivo_timer",
        "specivo_agent_session",
    ),
    PLANNING: (
        "specivo_list_version_issues",
        "specivo_create_version",
        "specivo_update_version",
        "specivo_delete_version",
        "specivo_list_sprint_issues",
        "specivo_create_sprint",
        "specivo_update_sprint",
        "specivo_start_sprint",
        "specivo_complete_sprint",
    ),
    RECURRING: (
        "specivo_list_recurring_patterns",
        "specivo_create_recurring_pattern",
        "specivo_update_recurring_pattern",
        "specivo_delete_recurring_pattern",
        "specivo_skip_recurrence_occurrence",
        "specivo_list_recurrence_occurrences",
    ),
    ADMIN: (
        "specivo_create_metadata_schema",
        "specivo_update_metadata_schema",
        "specivo_delete_metadata_schema",
        "specivo_create_tag",
        "specivo_update_tag",
        "specivo_delete_tag",
    ),
    AI_SPEND: (
        "specivo_log_ai_usage",
        "specivo_ai_usage",
        "specivo_ai_usage_report",
        "specivo_ai_subscriptions",
        "specivo_ai_billing_accounts",
        "specivo_ai_reimbursement_claims",
        "specivo_ai_spend_budgets",
    ),
}

#: One line per toolset, for the setup guide and error messages.
TOOLSET_SUMMARIES: dict[str, str] = {
    CORE: "issues, comments, relations, wiki, search and links, metadata and tags, time, lookups",
    PLANNING: "manage versions and sprints",
    RECURRING: "recurring task patterns",
    ADMIN: "manage metadata schemas and tags",
    AI_SPEND: "AI usage, spend reports, subscriptions, billing accounts, claims, budgets",
}

#: What a connection that names no toolsets gets.
DEFAULT_TOOLSETS: frozenset[str] = frozenset({CORE})

_TOOLSET_OF: dict[str, str] = {tool: name for name, tools in TOOLSETS.items() for tool in tools}

#: Toolsets for requests without an HTTP request of their own (an in-process client).
mcp_toolsets_var: contextvars.ContextVar[frozenset[str] | None] = contextvars.ContextVar("mcp_toolsets", default=None)


class UnknownToolsetError(ValueError):
    """The ``toolsets`` parameter names a toolset that does not exist."""


def parse_toolsets(value: str | None) -> frozenset[str]:
    """Parse a ``toolsets`` parameter: comma-separated names, or ``all``.

    Names are case-insensitive and surrounding blanks are ignored. ``None``
    or a blank value means ``DEFAULT_TOOLSETS``. An unknown name raises
    ``UnknownToolsetError`` naming the valid ones.
    """
    if value is None or not value.strip():
        return DEFAULT_TOOLSETS
    names = {part.strip().lower() for part in value.split(",") if part.strip()}
    if ALL in names:
        return frozenset(TOOLSETS)
    unknown = sorted(names - set(TOOLSETS))
    if unknown:
        quoted = ", ".join(f"'{name}'" for name in unknown)
        raise UnknownToolsetError(
            f"Unknown MCP toolset {quoted} in ?{TOOLSETS_PARAM}=. Valid toolsets: {', '.join(TOOLSETS)}, or {ALL}."
        )
    return frozenset(names)


def format_toolsets(toolsets: Iterable[str]) -> str:
    """The ``toolsets`` parameter value for *toolsets*, in canonical order."""
    selected = set(toolsets)
    if selected >= set(TOOLSETS):
        return ALL
    return ",".join(name for name in TOOLSETS if name in selected)


def toolset_of(tool: str) -> str | None:
    """The toolset *tool* belongs to, or ``None`` for a name that is in none."""
    return _TOOLSET_OF.get(tool)


def tools_in(toolsets: Iterable[str]) -> frozenset[str]:
    """The names of the tools in *toolsets*."""
    return frozenset(tool for name in toolsets for tool in TOOLSETS.get(name, ()))


def is_tool_enabled(tool: str, toolsets: frozenset[str]) -> bool:
    """Whether *tool* may be listed and called with *toolsets*.

    A tool in no toolset (registered by a plugin, say) is always enabled:
    hiding it would make it unreachable.
    """
    toolset = _TOOLSET_OF.get(tool)
    return toolset is None or toolset in toolsets


def hidden_tool_message(tool: str, toolsets: frozenset[str]) -> str:
    """The tool error for a call of *tool*, which *toolsets* leave out."""
    toolset = _TOOLSET_OF.get(tool, "")
    wanted = format_toolsets(toolsets | {toolset})
    return (
        f"{tool} is in the '{toolset}' toolset, which this MCP connection does not load. "
        f"Connect with ?{TOOLSETS_PARAM}={wanted} on the Specivo MCP URL "
        f"(for example /mcp/?{TOOLSETS_PARAM}={wanted}), or ?{TOOLSETS_PARAM}={ALL}."
    )


def toolsets_from_request_context(ctx: ServerRequestContext[Any, Any]) -> frozenset[str]:
    """The toolsets of the connection an MCP request came on.

    The ``toolsets`` query parameter of the HTTP request (Streamable HTTP, or
    the message endpoint of a legacy SSE stream) first, then ``mcp_toolsets_var``,
    then ``DEFAULT_TOOLSETS``.
    Raises ``UnknownToolsetError`` for an unknown name.
    """
    request = getattr(ctx, "request", None)
    query = getattr(request, "query_params", None) if request is not None else None
    value = query.get(TOOLSETS_PARAM) if query is not None else None
    if value is not None:
        return parse_toolsets(value)
    connection = mcp_toolsets_var.get()
    return connection if connection is not None else DEFAULT_TOOLSETS


class ToolsetMiddleware:
    """MCP server middleware that limits a connection to its toolsets.

    ``tools/list`` lists only the tools of the selected toolsets; a
    ``tools/call`` of another tool answers an ``isError`` result naming the
    toolset to add. An unknown toolset name refuses every request (including
    ``initialize``) with ``INVALID_PARAMS`` and a message listing the valid ones.
    """

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        if ctx.request_id is None:
            # A notification: nothing is listed or called.
            return await call_next(ctx)
        try:
            toolsets = toolsets_from_request_context(ctx)
        except UnknownToolsetError as exc:
            raise MCPError(code=INVALID_PARAMS, message=str(exc)) from None
        if ctx.method == "tools/call":
            name = (ctx.params or {}).get("name")
            if isinstance(name, str) and not is_tool_enabled(name, toolsets):
                message = hidden_tool_message(name, toolsets)
                return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)
            return await call_next(ctx)
        result = await call_next(ctx)
        if ctx.method == "tools/list" and isinstance(result, dict) and isinstance(result.get("tools"), list):
            result = {
                **result,
                "tools": [
                    tool
                    for tool in result["tools"]
                    if not isinstance(tool, dict) or is_tool_enabled(str(tool.get("name")), toolsets)
                ],
            }
        return result
