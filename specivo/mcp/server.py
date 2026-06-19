"""FastMCP server -- exposes Specivo tools over MCP protocol.

Authentication happens in two layers (see ``mcp.auth``):

1. Transport layer: ``SpvTokenVerifier`` validates the Bearer key on
   every HTTP request and stashes the raw key in a contextvar.
2. Tool layer: ``_get_session_and_user`` re-authenticates the raw key
   on **every** tool call, ensuring revoked / expired keys are blocked
   instantly -- even within a long-lived SSE session.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from specivo.core.database import get_session_factory
from specivo.mcp.auth import authenticate_mcp_tool
from specivo.mcp.docs import generate_setup_guide
from specivo.mcp.tools import (
    _add_comment,
    _add_relation,
    _append_wiki,
    _complete_sprint,
    _create_issue,
    _create_metadata_schema,
    _create_recurring_pattern,
    _create_sprint,
    _create_tag,
    _create_version,
    _create_wiki,
    _delete_metadata_schema,
    _delete_recurring_pattern,
    _delete_tag,
    _delete_version,
    _delete_wiki,
    _edit_description,
    _edit_wiki,
    _list_comments,
    _list_issues,
    _list_lookups,
    _list_members,
    _list_metadata_schemas,
    _list_projects,
    _list_recurrence_occurrences,
    _list_recurring_patterns,
    _list_relations,
    _list_sprint_issues,
    _list_sprints,
    _list_tags,
    _list_version_issues,
    _list_versions,
    _list_wiki_pages,
    _log_time,
    _metadata,
    _read_wiki,
    _read_wiki_section,
    _remove_relation,
    _replace_wiki_section,
    _restore_wiki,
    _search,
    _show_issue,
    _skip_recurrence_occurrence,
    _start_sprint,
    _tag,
    _update_issue,
    _update_metadata_schema,
    _update_recurring_pattern,
    _update_sprint,
    _update_tag,
    _update_version,
    _update_wiki_metadata,
    _whoami,
)
from specivo.services.agent_session_service import AgentSessionService
from specivo.services.permission_service import clear_role_cache

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "specivo",
    instructions=(
        "Specivo -- self-hosted platform for project tracking, knowledge base, and AI-safe automation. "
        "Call specivo_setup_guide() to get the full agent configuration guide, "
        "or read the specivo://docs/agent-setup resource."
    ),
)


_agent_session_svc = AgentSessionService()


@asynccontextmanager
async def _get_session_and_user() -> AsyncGenerator[tuple, None]:
    """Open a DB session, re-authenticate, and track AgentSession.

    The raw key was stashed by ``SpvTokenVerifier`` at transport level.
    Here we re-validate it against the DB on every tool call so that
    key revocation, expiry, or user deactivation takes effect immediately.

    The session is committed on clean exit and rolled back on error.
    """
    factory = get_session_factory()
    clear_role_cache()  # fresh permission state per tool call
    async with factory() as session:
        try:
            user, api_key = await authenticate_mcp_tool(session)
            # Track agent session
            try:
                await _agent_session_svc.get_or_create_session(session, api_key.id, user.id, user_agent=None)
            except Exception:
                logger.debug("AgentSession tracking failed", exc_info=True)
            yield session, user
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# MCP Resource
# ---------------------------------------------------------------------------


@mcp.resource(
    "specivo://docs/agent-setup",
    name="agent-setup",
    title="Specivo Agent Setup Guide",
    description=(
        "Dynamic setup guide for AI agents: key concepts, tool overview, standard workflows, and anti-patterns."
    ),
    mime_type="text/markdown",
)
async def agent_setup_resource() -> str:
    """Return the agent setup guide as an MCP resource."""
    return generate_setup_guide(fmt="generic", mcp_server=mcp)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


@mcp.tool()
async def specivo_whoami() -> str:
    """Return the authenticated user's identity.

    Returns user_id, login, display_name, email, is_admin, and status.
    Call this to discover your own user_id for self-assignment.
    """
    async with _get_session_and_user() as (session, user):
        return await _whoami(session, user)


@mcp.tool()
async def specivo_list_projects(
    offset: Annotated[int, Field(description="Number of projects to skip (pagination).")] = 0,
    limit: Annotated[int, Field(description="Max projects to return (1-100).")] = 25,
) -> str:
    """List all projects visible to the authenticated user.

    Returns project key, name, and status (active/archived).
    """
    async with _get_session_and_user() as (session, user):
        return await _list_projects(session, user, offset, limit)


@mcp.tool()
async def specivo_list_issues(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    status: Annotated[str, Field(description="Filter: 'open', 'all', or a status name.")] = "open",
    sort: Annotated[str, Field(description="Sort field:direction, e.g. 'created_at:desc'.")] = "created_at:desc",
    offset: Annotated[int, Field(description="Number of issues to skip (pagination).")] = 0,
    limit: Annotated[int, Field(description="Max issues to return (1-100).")] = 25,
    sprint_id: Annotated[
        int | None,
        Field(
            description=(
                "Filter to issues in this sprint. Use specivo_list_sprint_issues "
                "for sprint-specific listings with more fields."
            ),
        ),
    ] = None,
    metadata_filters: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "Filter by metadata as 'key=value' strings, e.g. "
                "['component=frontend', 'priority-tag=p0']. AND-combined. Each "
                "pair matches when the metadata value at *key* equals *value* "
                "(scalar) or when the array stored at *key* contains *value*. "
                "Discover available keys via specivo_list_metadata_schemas."
            ),
        ),
    ] = None,
) -> str:
    """List issues for a project with filtering and sorting.

    Use status='open' (default) for active issues, 'all' for everything.
    Pass sprint_id to narrow the results to a single sprint.
    Pass metadata_filters to narrow by JSONB metadata (e.g. component=frontend).
    """
    async with _get_session_and_user() as (session, user):
        return await _list_issues(
            session, user, project_key, status, sort, offset, limit, sprint_id, metadata_filters,
        )


@mcp.tool()
async def specivo_show_issue(
    issue_ref: Annotated[str, Field(description="Display key, e.g. ACME-12. Never pass a numeric ID.")],
    metadata_only: Annotated[bool, Field(description="If true, skip description body to save tokens.")] = False,
    search: Annotated[str | None, Field(description="Return only the description section matching this text.")] = None,
) -> str:
    """Show full issue details including description.

    Use metadata_only=true to skip the description body and save tokens.
    Use search= to extract only the section containing specific text.
    """
    async with _get_session_and_user() as (session, user):
        return await _show_issue(session, user, issue_ref, metadata_only, search)


@mcp.tool()
async def specivo_create_issue(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    tracker_id: Annotated[int, Field(description="Tracker ID from specivo_list_lookups (e.g. 1=Bug, 2=Feature).")],
    subject: Annotated[str, Field(description="Issue title/subject line.")],
    description: Annotated[str, Field(description="Markdown body. Can be empty.")] = "",
    status_id: Annotated[
        int | None, Field(description="Status ID from list_lookups. Defaults to tracker default.")
    ] = None,
    priority_id: Annotated[
        int | None, Field(description="Priority ID from list_lookups. Defaults to system default.")
    ] = None,
    assigned_to_id: Annotated[
        int | None, Field(description="User ID from list_members. Use whoami for self-assign.")
    ] = None,
    fixed_version_id: Annotated[int | None, Field(description="Version ID from list_versions.")] = None,
    sprint_id: Annotated[int | None, Field(description="Sprint ID from list_sprints.")] = None,
) -> str:
    """Create a new issue in a project.

    Call specivo_list_lookups first to get valid tracker_id and priority_id.
    Call specivo_list_members to find user IDs for assignment.
    """
    async with _get_session_and_user() as (session, user):
        return await _create_issue(
            session,
            user,
            project_key,
            tracker_id,
            subject,
            description,
            status_id,
            priority_id,
            assigned_to_id,
            fixed_version_id,
            sprint_id,
        )


@mcp.tool()
async def specivo_update_issue(
    issue_ref: Annotated[str, Field(description="Display key, e.g. ACME-12.")],
    subject: Annotated[str | None, Field(description="New subject line.")] = None,
    description: Annotated[
        str | None, Field(description="Full replacement. Prefer edit_description for patches.")
    ] = None,
    status_id: Annotated[int | None, Field(description="Status ID from list_lookups.")] = None,
    priority_id: Annotated[int | None, Field(description="Priority ID from list_lookups.")] = None,
    assigned_to_id: Annotated[int | None, Field(description="User ID from list_members.")] = None,
    done_ratio: Annotated[int | None, Field(description="Completion percentage (0-100).")] = None,
    notes: Annotated[str | None, Field(description="Journal note (appears in issue history).")] = None,
    fixed_version_id: Annotated[int | None, Field(description="Version ID from list_versions.")] = None,
    sprint_id: Annotated[int | None, Field(description="Sprint ID from list_sprints.")] = None,
) -> str:
    """Update an issue. Lock version is handled automatically.

    Only pass fields you want to change; others remain unchanged.
    For description patches, prefer specivo_edit_description instead.
    """
    async with _get_session_and_user() as (session, user):
        return await _update_issue(
            session,
            user,
            issue_ref,
            subject,
            description,
            status_id,
            priority_id,
            assigned_to_id,
            done_ratio,
            notes,
            fixed_version_id,
            sprint_id,
        )


@mcp.tool()
async def specivo_edit_description(
    issue_ref: Annotated[str, Field(description="Display key, e.g. ACME-12.")],
    search_text: Annotated[
        str, Field(description="Exact text to find (first occurrence). Use show_issue(search=) to locate.")
    ],
    replace_text: Annotated[str, Field(description="Replacement text.")],
) -> str:
    """Search-and-replace in issue description. Token-efficient editing.

    Replaces only the first occurrence. Returns an error message (not exception) if search_text is not found.
    Use specivo_show_issue(issue_ref, search='keyword') first to find the exact text.
    """
    async with _get_session_and_user() as (session, user):
        return await _edit_description(session, user, issue_ref, search_text, replace_text)


@mcp.tool()
async def specivo_search(
    query: Annotated[str, Field(description="Search query.")],
    project_key: Annotated[str | None, Field(description="Limit to this project. Uppercase, e.g. ACME.")] = None,
    scope: Annotated[str, Field(description="'all', 'issues', or 'wiki'.")] = "all",
    limit: Annotated[int, Field(description="Max results to return (1-50).")] = 10,
    mode: Annotated[
        str,
        Field(
            description=(
                "Search mode: 'hybrid' (default, FTS + semantic with RRF fusion), "
                "'keyword' (tsvector FTS only — fast, exact-match), "
                "'semantic' (pgvector embeddings — conceptual matches). "
                "Use 'hybrid' unless you need a specific behavior."
            )
        ),
    ] = "hybrid",
) -> str:
    """Search across issues and wiki pages.

    Defaults to hybrid search, which fuses full-text (tsvector) and semantic
    (pgvector) results via reciprocal-rank fusion — best general-purpose recall.
    Use mode='keyword' for exact-text/identifier queries or mode='semantic'
    for conceptual lookups.

    Returns results with type, title, subtitle, and snippet.
    Use project_key to narrow results to a single project.
    """
    async with _get_session_and_user() as (session, user):
        return await _search(session, user, query, project_key, scope, limit, mode)


@mcp.tool()
async def specivo_read_wiki(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    slug: Annotated[str, Field(description="Wiki page slug from list_wiki_pages, e.g. 'architecture-overview'.")],
    metadata_only: Annotated[bool, Field(description="If true, skip body content to save tokens.")] = False,
    search: Annotated[str | None, Field(description="Return only the section containing this text.")] = None,
) -> str:
    """Read a wiki page by its slug.

    Use specivo_list_wiki_pages to discover available slugs.
    Use search= to extract only the section containing specific text from long pages.
    """
    async with _get_session_and_user() as (session, user):
        return await _read_wiki(session, user, project_key, slug, metadata_only, search)


@mcp.tool()
async def specivo_list_wiki_pages(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
) -> str:
    """List all wiki pages for a project.

    Returns slug and title for each page. Use the slug with specivo_read_wiki.
    """
    async with _get_session_and_user() as (session, user):
        return await _list_wiki_pages(session, user, project_key)


@mcp.tool()
async def specivo_edit_wiki(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    slug: Annotated[str, Field(description="Wiki page slug.")],
    search_text: Annotated[str, Field(description="Exact text to find (first occurrence).")],
    replace_text: Annotated[str, Field(description="Replacement text.")],
) -> str:
    """Search-and-replace in wiki page content. Token-efficient editing.

    Replaces only the first occurrence. Returns an error message (not exception) if search_text is not found.
    Use specivo_read_wiki first to find the exact text.
    """
    async with _get_session_and_user() as (session, user):
        return await _edit_wiki(session, user, project_key, slug, search_text, replace_text)


@mcp.tool()
async def specivo_append_wiki(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    slug: Annotated[str, Field(description="Wiki page slug.")],
    text: Annotated[str, Field(description="Markdown text to append.")],
    position: Annotated[
        str,
        Field(description="Where to insert: 'end' (default) or 'after:## Heading Name' to insert after a section."),
    ] = "end",
) -> str:
    """Append text to a wiki page. Token-efficient for building large pages incrementally.

    Use position='end' to append at the bottom, or position='after:## Section Name'
    to insert after a specific heading. The text is inserted before the next
    same-or-higher-level heading.
    """
    async with _get_session_and_user() as (session, user):
        return await _append_wiki(session, user, project_key, slug, text, position)


@mcp.tool()
async def specivo_read_wiki_section(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    slug: Annotated[str, Field(description="Wiki page slug.")],
    heading: Annotated[
        str,
        Field(description="Section heading, e.g. '## Architecture' or just 'Architecture'."),
    ],
    include_children: Annotated[
        bool,
        Field(description="If true (default), include sub-headings. If false, stop at the first sub-heading."),
    ] = True,
) -> str:
    """Read a single section from a wiki page by heading. Saves tokens on large pages.

    Accepts both '## Foo' (exact heading) and 'Foo' (searches all levels).
    Use specivo_read_wiki(metadata_only=true) first to check the page exists,
    then read specific sections.
    """
    async with _get_session_and_user() as (session, user):
        return await _read_wiki_section(
            session,
            user,
            project_key,
            slug,
            heading,
            include_children,
        )


@mcp.tool()
async def specivo_replace_wiki_section(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    slug: Annotated[str, Field(description="Wiki page slug.")],
    heading: Annotated[
        str,
        Field(description="Section heading to replace, e.g. '## Architecture' or just 'Architecture'."),
    ],
    text: Annotated[str, Field(description="New section body (the heading line is preserved).")],
) -> str:
    """Replace a section's body in a wiki page while preserving the heading line.

    Replaces everything between the heading and the next same-or-higher-level heading.
    Use specivo_read_wiki_section first to see the current content.
    """
    async with _get_session_and_user() as (session, user):
        return await _replace_wiki_section(session, user, project_key, slug, heading, text)


@mcp.tool()
async def specivo_add_comment(
    issue_ref: Annotated[str, Field(description="Display key, e.g. ACME-12.")],
    notes: Annotated[str, Field(description="Comment text (Markdown supported).")],
) -> str:
    """Add a comment (journal note) to an issue.

    The comment appears in the issue's history/activity feed.
    """
    async with _get_session_and_user() as (session, user):
        return await _add_comment(session, user, issue_ref, notes)


@mcp.tool()
async def specivo_list_comments(
    issue_ref: Annotated[str, Field(description="Display key, e.g. ACME-12.")],
    limit: Annotated[int, Field(description="Max comments to return (1..50).", ge=1, le=50)] = 10,
    offset: Annotated[int, Field(description="Pagination offset (>=0).", ge=0)] = 0,
    order: Annotated[str, Field(description="Order by created_at: 'asc' or 'desc'.")] = "desc",
) -> str:
    """List comments (notes-only journals) on an issue, paginated.

    Pure field-change journals are excluded; only real user comments are
    returned. Use specivo_show_issue to see the total count first, then
    page through with limit/offset.
    """
    async with _get_session_and_user() as (session, user):
        return await _list_comments(session, user, issue_ref, limit, offset, order)


@mcp.tool()
async def specivo_create_wiki(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    title: Annotated[str, Field(description="Page title. Slug is auto-derived from this.")],
    text: Annotated[str, Field(description="Page content (Markdown).")],
    parent_slug: Annotated[str | None, Field(description="Parent page slug for nested hierarchy.")] = None,
) -> str:
    """Create a new wiki page. Slug is auto-derived from the title.

    Use parent_slug to nest under an existing page.
    """
    async with _get_session_and_user() as (session, user):
        return await _create_wiki(session, user, project_key, title, text, parent_slug)


@mcp.tool()
async def specivo_update_wiki_metadata(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    slug: Annotated[str, Field(description="Wiki page slug to update.")],
    parent_slug: Annotated[
        str | None,
        Field(description="New parent page slug. Empty string '' to make root-level. None to leave unchanged."),
    ] = None,
    title: Annotated[
        str | None,
        Field(description="New title. Renames the page and creates a redirect from the old slug."),
    ] = None,
    protected: Annotated[
        bool | None,
        Field(description="Set protected flag. None to leave unchanged."),
    ] = None,
) -> str:
    """Update wiki page metadata (parent, title, protected flag) without editing content.

    At least one of parent_slug, title, or protected must be provided.
    For title changes, a redirect from the old slug is created automatically.
    Set parent_slug to empty string '' to make the page root-level.
    """
    async with _get_session_and_user() as (session, user):
        return await _update_wiki_metadata(session, user, project_key, slug, parent_slug, title, protected)


@mcp.tool()
async def specivo_delete_wiki(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    slug: Annotated[str, Field(description="Wiki page slug to delete.")],
    cascade_children: Annotated[
        bool, Field(description="If true, also delete all child pages. If false, re-parent children.")
    ] = False,
) -> str:
    """Soft-delete a wiki page (moves to trash).

    The Home page cannot be deleted. If cascade_children is true, all
    descendant pages are also deleted. Otherwise children are re-parented.
    Deleted pages can be restored with specivo_restore_wiki.
    """
    async with _get_session_and_user() as (session, user):
        return await _delete_wiki(session, user, project_key, slug, cascade_children)


@mcp.tool()
async def specivo_restore_wiki(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    slug: Annotated[str, Field(description="Slug of the deleted wiki page to restore.")],
    cascade: Annotated[bool, Field(description="If true (default), also restore co-deleted child pages.")] = True,
) -> str:
    """Restore a soft-deleted wiki page from trash.

    Fails if an active page with the same slug already exists.
    Use specivo_list_wiki_pages to check before restoring.
    """
    async with _get_session_and_user() as (session, user):
        return await _restore_wiki(session, user, project_key, slug, cascade)


@mcp.tool()
async def specivo_list_lookups() -> str:
    """List all trackers, statuses, priorities, and time entry activities with their IDs.

    Call this before specivo_create_issue or specivo_log_time to get valid ID values.
    IDs are instance-specific and must not be assumed or hardcoded.
    """
    async with _get_session_and_user() as (session, user):
        return await _list_lookups(session, user)


@mcp.tool()
async def specivo_list_members(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
) -> str:
    """List project members with their roles and user IDs.

    Use this to find assigned_to_id values for specivo_create_issue / specivo_update_issue.
    """
    async with _get_session_and_user() as (session, user):
        return await _list_members(session, user, project_key)


@mcp.tool()
async def specivo_list_metadata_schemas(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    tracker_id: Annotated[int | None, Field(description="Filter to schemas for this tracker ID.")] = None,
    content_type: Annotated[
        str | None,
        Field(description="Filter to a single content type (e.g. 'issue'). None = all types."),
    ] = None,
) -> str:
    """Discover custom metadata schemas for a project.

    Call this before creating or updating issues to learn what metadata
    fields are available. When tracker_id is given, returns schemas
    applicable to that specific tracker. When content_type is given,
    returns only schemas targeting that content kind.
    """
    async with _get_session_and_user() as (session, user):
        return await _list_metadata_schemas(session, user, project_key, tracker_id, content_type)


@mcp.tool()
async def specivo_create_metadata_schema(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    name: Annotated[str, Field(description="Human-readable schema name, unique per (project, tracker, content_type).")],
    schema: Annotated[
        dict,
        Field(description="Full JSON Schema body (the schema_definition). Must be a valid Draft 2020-12 schema."),
    ],
    content_type: Annotated[
        str,
        Field(description="Entity type the schema applies to. Defaults to 'issue'."),
    ] = "issue",
    tracker_id: Annotated[
        int | None,
        Field(description="Restrict schema to a single tracker ID. None = applies to all trackers in the project."),
    ] = None,
    description: Annotated[
        str | None,
        Field(description="Optional human description shown in admin UI."),
    ] = None,
) -> str:
    """Create a metadata schema for a project.

    Requires the 'manage_project' permission on the target project.
    The mutation is recorded in the security audit log
    (METADATA_SCHEMA_CREATED event).

    Use specivo_list_metadata_schemas first to see existing schemas
    and avoid duplicate names.
    """
    async with _get_session_and_user() as (session, user):
        return await _create_metadata_schema(
            session,
            user,
            project_key,
            name,
            schema,
            content_type=content_type,
            tracker_id=tracker_id,
            description=description,
        )


@mcp.tool()
async def specivo_update_metadata_schema(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    schema_id: Annotated[int, Field(description="Numeric schema ID from specivo_list_metadata_schemas.")],
    name: Annotated[str | None, Field(description="New name. Omit to keep current.")] = None,
    tracker_id: Annotated[
        int | None,
        Field(description="New tracker_id. Omit to keep current. Pass null to clear (project-wide)."),
    ] = None,
    schema: Annotated[
        dict | None,
        Field(description="Replacement JSON Schema body. Omit to keep current."),
    ] = None,
    description: Annotated[
        str | None,
        Field(description="New description. Omit to keep current."),
    ] = None,
) -> str:
    """Patch a metadata schema.

    Requires the 'manage_project' permission on the target project.
    The mutation is recorded in the security audit log
    (METADATA_SCHEMA_UPDATED event).

    Only provided fields are applied; omitted fields are left unchanged.
    """
    async with _get_session_and_user() as (session, user):
        return await _update_metadata_schema(
            session,
            user,
            project_key,
            schema_id,
            name=name,
            tracker_id=tracker_id,
            schema=schema,
            description=description,
        )


@mcp.tool()
async def specivo_delete_metadata_schema(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    schema_id: Annotated[int, Field(description="Numeric schema ID from specivo_list_metadata_schemas.")],
) -> str:
    """Delete a metadata schema.

    Requires the 'manage_project' permission on the target project.
    The mutation is recorded in the security audit log
    (METADATA_SCHEMA_DELETED event).

    Fails with a conflict error if any issue still has metadata
    matching the schema's defined keys (safe-delete behaviour).
    """
    async with _get_session_and_user() as (session, user):
        return await _delete_metadata_schema(session, user, project_key, schema_id)


@mcp.tool()
async def specivo_metadata(
    target_ref: Annotated[
        str,
        Field(
            description=(
                "Scheme-prefixed entity reference, e.g. 'issue:ACME-12'. "
                "Bare issue refs like 'ACME-12' are accepted for backward compatibility."
            ),
        ),
    ],
    key: Annotated[str, Field(description="Metadata key to mutate.")],
    op: Annotated[
        str,
        Field(description="One of: set, get, delete, append, remove."),
    ],
    value: Annotated[
        Any,
        Field(
            default=None,
            description=(
                "Value to apply. For 'set', any JSON value. "
                "For 'append'/'remove', a scalar or list. Ignored for 'get' and 'delete'."
            ),
        ),
    ] = None,
) -> str:
    """Read or mutate a single metadata key on an entity.

    Supported operations:
    - set: metadata[key] = value (any JSON value)
    - get: return metadata[key] as JSON, or '(not set)' if missing
    - delete: remove key (silent no-op if missing)
    - append: append to a list at key (missing key -> new list)
    - remove: drop matching items from a list at key

    The 'get' op is read-only and requires only view_issues permission;
    all other ops require edit_issues. Use specivo_list_metadata_schemas
    first to discover available fields. Recoverable errors are returned
    as 'Error: ...' strings.
    """
    async with _get_session_and_user() as (session, user):
        return await _metadata(session, user, target_ref, key, op, value)


@mcp.tool()
async def specivo_list_relations(
    issue_ref: Annotated[str, Field(description="Display key, e.g. ACME-12.")],
) -> str:
    """List all relations for an issue.

    Returns relation ID, type, and target issue key.
    Use the relation ID with specivo_remove_relation.
    """
    async with _get_session_and_user() as (session, user):
        return await _list_relations(session, user, issue_ref)


@mcp.tool()
async def specivo_add_relation(
    issue_ref: Annotated[str, Field(description="Source issue display key, e.g. ACME-12.")],
    issue_to_key: Annotated[str, Field(description="Target issue display key, e.g. ACME-15.")],
    relation_type: Annotated[
        str,
        Field(description="relates|blocks|blocked|duplicates|duplicated|precedes|follows|copied_to|copied_from."),
    ],
    delay: Annotated[int | None, Field(description="Delay in days. Only meaningful for precedes/follows.")] = None,
) -> str:
    """Create a relation between two issues.

    Relation types: relates, blocks, blocked, duplicates, duplicated,
    precedes, follows, copied_to, copied_from.
    """
    async with _get_session_and_user() as (session, user):
        return await _add_relation(session, user, issue_ref, issue_to_key, relation_type, delay)


@mcp.tool()
async def specivo_remove_relation(
    issue_ref: Annotated[str, Field(description="Issue display key, e.g. ACME-12.")],
    relation_id: Annotated[int, Field(description="Relation ID from specivo_list_relations.")],
) -> str:
    """Remove a relation by its ID.

    Use specivo_list_relations first to find the relation ID.
    """
    async with _get_session_and_user() as (session, user):
        return await _remove_relation(session, user, issue_ref, relation_id)


@mcp.tool()
async def specivo_log_time(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    hours: Annotated[float, Field(description="Decimal hours, e.g. 1.5 for 90 minutes.")],
    activity_id: Annotated[int, Field(description="Activity ID from specivo_list_lookups.")],
    issue_ref: Annotated[str | None, Field(description="Optional issue display key, e.g. ACME-12.")] = None,
    comments: Annotated[str | None, Field(description="Description of work performed.")] = None,
    spent_on: Annotated[str | None, Field(description="ISO date YYYY-MM-DD. Defaults to today.")] = None,
) -> str:
    """Log time against a project and optionally a specific issue.

    Call specivo_list_lookups first to get a valid activity_id.
    """
    from datetime import date
    from decimal import Decimal

    parsed_date: date | None = date.fromisoformat(spent_on) if spent_on else None

    async with _get_session_and_user() as (session, user):
        return await _log_time(
            session,
            user,
            project_key,
            Decimal(str(hours)),
            activity_id,
            issue_ref,
            comments,
            parsed_date,
        )


@mcp.tool()
async def specivo_list_versions(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
) -> str:
    """List project versions/milestones with status and due dates.

    Use the version ID with specivo_create_issue(fixed_version_id=) or specivo_update_issue(fixed_version_id=).
    """
    async with _get_session_and_user() as (session, user):
        return await _list_versions(session, user, project_key)


@mcp.tool()
async def specivo_list_version_issues(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    version_id: Annotated[
        int,
        Field(description="Version ID from list_versions. Use 0 for unversioned issues."),
    ],
    fields: Annotated[
        str,
        Field(
            description=(
                "Field level: 'minimal' (key+subject), "
                "'default' (+status/tracker/priority/assignee), "
                "'full' (+description/done/metadata)."
            ),
        ),
    ] = "default",
    offset: Annotated[int, Field(description="Number of issues to skip (pagination).")] = 0,
    limit: Annotated[int, Field(description="Max issues to return (1-100).")] = 25,
) -> str:
    """List issues assigned to a version/release, or unversioned issues (version_id=0).

    Field levels control output verbosity to save tokens:
    - minimal: display_key and subject only
    - default: adds status, tracker, priority, assigned_to
    - full: adds description (first 200 chars), done_ratio, sprint_id, metadata
    """
    async with _get_session_and_user() as (session, user):
        return await _list_version_issues(session, user, project_key, version_id, fields, offset, limit)


@mcp.tool()
async def specivo_create_version(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    name: Annotated[str, Field(description="Version name, e.g. 'v1.2.0'.")],
    description: Annotated[str | None, Field(description="Version description.")] = None,
    status: Annotated[str, Field(description="One of: open, locked, closed.")] = "open",
    due_date: Annotated[str | None, Field(description="Due date in YYYY-MM-DD format.")] = None,
) -> str:
    """Create a new version/milestone in a project.

    Status can be: open (default), locked, or closed.
    """
    from datetime import date

    parsed_date: date | None = date.fromisoformat(due_date) if due_date else None

    async with _get_session_and_user() as (session, user):
        return await _create_version(session, user, project_key, name, description, status, parsed_date)


@mcp.tool()
async def specivo_update_version(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    version_id: Annotated[int, Field(description="Version ID from specivo_list_versions.")],
    name: Annotated[str | None, Field(description="New version name.")] = None,
    description: Annotated[str | None, Field(description="New description.")] = None,
    status: Annotated[str | None, Field(description="One of: open, locked, closed.")] = None,
    due_date: Annotated[str | None, Field(description="Due date in YYYY-MM-DD format.")] = None,
) -> str:
    """Update an existing version/milestone.

    Only pass fields you want to change.
    """
    from datetime import date

    parsed_date: date | None = date.fromisoformat(due_date) if due_date else None

    async with _get_session_and_user() as (session, user):
        return await _update_version(
            session,
            user,
            project_key,
            version_id,
            name,
            description,
            status,
            parsed_date,
        )


@mcp.tool()
async def specivo_delete_version(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    version_id: Annotated[int, Field(description="Version ID from specivo_list_versions.")],
) -> str:
    """Delete a version/milestone.

    Fails with an error message if any issues still reference this version.
    Reassign or clear fixed_version_id on those issues first.
    """
    async with _get_session_and_user() as (session, user):
        return await _delete_version(session, user, project_key, version_id)


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


@mcp.tool()
async def specivo_list_tags(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
) -> str:
    """List a project's tags with their colors and usage counts.

    Tags are flat, case-insensitively-unique labels shared by issues and wiki
    pages. Use a tag ID with specivo_update_tag / specivo_delete_tag.
    """
    async with _get_session_and_user() as (session, user):
        return await _list_tags(session, user, project_key)


@mcp.tool()
async def specivo_create_tag(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    name: Annotated[str, Field(description="Tag name (unique per project, case-insensitive).")],
    color: Annotated[str | None, Field(description="Optional hex color, e.g. '#4f9d6c'.")] = None,
) -> str:
    """Create a project tag. Requires the 'manage_project' permission.

    Members can also create tags on the fly via specivo_tag(op='add'); this
    tool is for explicit vocabulary curation.
    """
    async with _get_session_and_user() as (session, user):
        return await _create_tag(session, user, project_key, name, color)


@mcp.tool()
async def specivo_update_tag(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    tag_id: Annotated[int, Field(description="Tag ID from specivo_list_tags.")],
    name: Annotated[str | None, Field(description="New tag name.")] = None,
    color: Annotated[str | None, Field(description="New hex color, e.g. '#4f9d6c'.")] = None,
) -> str:
    """Rename or recolor a tag. Requires the 'manage_project' permission."""
    async with _get_session_and_user() as (session, user):
        return await _update_tag(session, user, project_key, tag_id, name, color)


@mcp.tool()
async def specivo_delete_tag(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    tag_id: Annotated[int, Field(description="Tag ID from specivo_list_tags.")],
) -> str:
    """Delete a tag and detach it from all entities. Requires 'manage_project'."""
    async with _get_session_and_user() as (session, user):
        return await _delete_tag(session, user, project_key, tag_id)


@mcp.tool()
async def specivo_tag(
    target_ref: Annotated[
        str,
        Field(
            description=(
                "Entity to tag: an issue ref ('ACME-12' or 'issue:ACME-12') "
                "or a wiki page ('wiki:ACME/some-slug')."
            ),
        ),
    ],
    op: Annotated[str, Field(description="One of: get, add, remove, set.")],
    value: Annotated[
        Any,
        Field(
            default=None,
            description=(
                "Tag name or list of names. For 'set', the full desired list. "
                "Ignored for 'get'. New names are created on the fly for add/set."
            ),
        ),
    ] = None,
) -> str:
    """Read or mutate the tags on an issue or wiki page.

    Ops: get (list current tags), add (apply one or more, creating new tags as
    needed), remove (detach by name), set (replace the full tag set). Requires
    project access; mutations are recorded in the audit log.
    """
    async with _get_session_and_user() as (session, user):
        return await _tag(session, user, target_ref, op, value)


# ---------------------------------------------------------------------------
# Recurring patterns
# ---------------------------------------------------------------------------


@mcp.tool()
async def specivo_list_recurring_patterns(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
) -> str:
    """List recurring task patterns for a project with rule, anchor mode and next occurrence.

    Requires the 'view_issues' permission. Use a pattern ID with the other
    recurring-pattern tools (update, delete, skip, list occurrences).
    """
    async with _get_session_and_user() as (session, user):
        return await _list_recurring_patterns(session, user, project_key)


@mcp.tool()
async def specivo_create_recurring_pattern(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    name: Annotated[str, Field(description="Human-readable pattern name, e.g. 'Weekly status report'.")],
    template_subject: Annotated[
        str,
        Field(
            description=(
                "Subject line for every generated issue. Supports date macros that "
                "expand per occurrence: {{year}}, {{quarter}}, {{month}}, "
                "{{month_num}}, {{day}}, {{weekday}} — e.g. '{{month}} {{year}} report'."
            )
        ),
    ],
    template_tracker_id: Annotated[
        int, Field(description="Tracker ID for generated issues (from specivo_list_lookups).")
    ],
    freq: Annotated[
        str,
        Field(description="Recurrence frequency: one of daily, weekly, monthly, yearly."),
    ],
    dtstart: Annotated[
        str,
        Field(
            description=(
                "Series anchor as an ISO-8601 datetime, e.g. '2026-01-05T09:00:00'. "
                "Interpreted in the pattern timezone; naive input is treated as UTC."
            )
        ),
    ],
    rrule_interval: Annotated[
        int, Field(description="Interval between occurrences (e.g. 2 = every other week).")
    ] = 1,
    byday: Annotated[
        str | None,
        Field(
            description=(
                "Comma-separated weekday tokens for weekly/monthly rules, e.g. 'MO,WE,FR' "
                "or positional '1MO,-1FR'. Leave empty for daily/simple rules."
            )
        ),
    ] = None,
    rrule_count: Annotated[
        int | None, Field(description="Total number of occurrences before the series ends.")
    ] = None,
    rrule_raw: Annotated[
        str | None,
        Field(
            description=(
                "Escape hatch: a full RFC 5545 RRULE string (e.g. "
                "'FREQ=MONTHLY;BYDAY=3WE'). When set it overrides the structured "
                "BY* fields. Prefer the structured params unless you need a rule "
                "they cannot express."
            )
        ),
    ] = None,
    anchor_mode: Annotated[
        str,
        Field(
            description=(
                "'fixed' = occurrences fire on the calendar schedule regardless of "
                "completion (overdue instances stack up). 'flexible' = the next "
                "instance is only generated after the previous one is closed. Choose "
                "deliberately: 'fixed' for calendar events, 'flexible' for chores."
            )
        ),
    ] = "fixed",
    base_date_strategy: Annotated[
        str,
        Field(
            description=(
                "For flexible mode: 'scheduled' anchors the next occurrence on the "
                "schedule; 'completion' anchors it on when the prior instance closed."
            )
        ),
    ] = "scheduled",
    timezone: Annotated[
        str,
        Field(
            description=(
                "IANA timezone for occurrence computation, e.g. 'America/New_York'. "
                "Set this correctly: it determines local occurrence dates and DST "
                "handling. Defaults to UTC."
            )
        ),
    ] = "UTC",
    template_description: Annotated[
        str | None,
        Field(
            description=(
                "Description applied to every generated issue. Supports the same date "
                "macros as template_subject ({{year}}, {{month}}, {{weekday}}, ...)."
            )
        ),
    ] = None,
    template_priority_id: Annotated[
        int | None, Field(description="Priority ID for generated issues (from specivo_list_lookups).")
    ] = None,
    template_assigned_to_id: Annotated[
        int | None, Field(description="User ID to assign generated issues to.")
    ] = None,
    creation_lead_time_days: Annotated[
        int,
        Field(description="How many days ahead occurrences are materialised into issues."),
    ] = 30,
    enabled: Annotated[
        bool, Field(description="Whether the pattern actively generates issues.")
    ] = True,
) -> str:
    """Create a recurring task pattern that generates issues on a schedule.

    Requires the 'manage_recurring_tasks' permission.

    Set anchor_mode deliberately ('fixed' for calendar-driven series, 'flexible'
    for complete-then-recur chores) and timezone to the correct IANA zone. Use
    the structured freq/rrule_interval/byday fields for common rules; byday is a
    comma-separated token list ('MO,WE,FR'). For rules the structured fields
    cannot express, pass a full RFC 5545 string via rrule_raw.

    The template_subject and template_description support date macros that expand
    for each generated occurrence (in the pattern timezone), so issues are not
    identical duplicates: {{year}}, {{quarter}}, {{month}}, {{month_num}}, {{day}},
    {{weekday}}. Month and weekday names are localized to the workspace language.
    """
    async with _get_session_and_user() as (session, user):
        return await _create_recurring_pattern(
            session,
            user,
            project_key,
            name,
            template_subject,
            template_tracker_id,
            freq,
            dtstart,
            rrule_interval,
            byday,
            rrule_count,
            rrule_raw,
            anchor_mode,
            base_date_strategy,
            timezone,
            template_description,
            template_priority_id,
            template_assigned_to_id,
            creation_lead_time_days,
            enabled,
        )


@mcp.tool()
async def specivo_update_recurring_pattern(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    pattern_id: Annotated[int, Field(description="Pattern ID from specivo_list_recurring_patterns.")],
    name: Annotated[str | None, Field(description="New pattern name.")] = None,
    template_subject: Annotated[
        str | None, Field(description="New subject for generated issues.")
    ] = None,
    template_description: Annotated[
        str | None, Field(description="New description for generated issues.")
    ] = None,
    freq: Annotated[
        str | None, Field(description="New frequency: daily, weekly, monthly, or yearly.")
    ] = None,
    rrule_interval: Annotated[int | None, Field(description="New interval between occurrences.")] = None,
    byday: Annotated[
        str | None,
        Field(description="New comma-separated weekday tokens, e.g. 'MO,WE,FR'."),
    ] = None,
    rrule_count: Annotated[int | None, Field(description="New total occurrence count.")] = None,
    rrule_raw: Annotated[
        str | None, Field(description="New raw RFC 5545 RRULE string (overrides BY* fields).")
    ] = None,
    anchor_mode: Annotated[
        str | None, Field(description="New anchor mode: 'fixed' or 'flexible'.")
    ] = None,
    base_date_strategy: Annotated[
        str | None, Field(description="New base-date strategy: 'scheduled' or 'completion'.")
    ] = None,
    dtstart: Annotated[
        str | None, Field(description="New ISO-8601 series anchor datetime.")
    ] = None,
    timezone: Annotated[str | None, Field(description="New IANA timezone.")] = None,
    creation_lead_time_days: Annotated[
        int | None, Field(description="New look-ahead window in days.")
    ] = None,
    enabled: Annotated[bool | None, Field(description="Enable or disable the pattern.")] = None,
) -> str:
    """Update a recurring task pattern. Only pass fields you want to change.

    Requires the 'manage_recurring_tasks' permission. Editing a pattern affects
    future occurrences only; issues already generated are left untouched.
    """
    async with _get_session_and_user() as (session, user):
        return await _update_recurring_pattern(
            session,
            user,
            project_key,
            pattern_id,
            name,
            template_subject,
            template_description,
            freq,
            rrule_interval,
            byday,
            rrule_count,
            rrule_raw,
            anchor_mode,
            base_date_strategy,
            dtstart,
            timezone,
            creation_lead_time_days,
            enabled,
        )


@mcp.tool()
async def specivo_delete_recurring_pattern(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    pattern_id: Annotated[int, Field(description="Pattern ID from specivo_list_recurring_patterns.")],
) -> str:
    """Delete a recurring task pattern.

    Requires the 'manage_recurring_tasks' permission. Issues already generated by
    the pattern survive (their link is cleared); skip/override exceptions are removed.
    """
    async with _get_session_and_user() as (session, user):
        return await _delete_recurring_pattern(session, user, project_key, pattern_id)


@mcp.tool()
async def specivo_skip_recurrence_occurrence(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    pattern_id: Annotated[int, Field(description="Pattern ID from specivo_list_recurring_patterns.")],
    occurrence_at: Annotated[
        str,
        Field(
            description=(
                "The scheduled occurrence to skip, as an ISO-8601 datetime matching "
                "a value from specivo_list_recurrence_occurrences."
            )
        ),
    ],
) -> str:
    """Skip a single scheduled occurrence (EXDATE) of a recurring pattern.

    Requires the 'manage_recurring_tasks' permission. The occurrence will not be
    generated and does not count as a completion. If an untouched issue was
    already generated for it, that issue is removed.
    """
    async with _get_session_and_user() as (session, user):
        return await _skip_recurrence_occurrence(session, user, project_key, pattern_id, occurrence_at)


@mcp.tool()
async def specivo_list_recurrence_occurrences(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    pattern_id: Annotated[int, Field(description="Pattern ID from specivo_list_recurring_patterns.")],
    days: Annotated[
        int | None,
        Field(
            description=(
                "Look-ahead window in days from now. Defaults to the pattern's "
                "creation_lead_time_days; capped at the server's configured maximum."
            )
        ),
    ] = None,
) -> str:
    """Preview upcoming occurrences of a recurring pattern without generating issues.

    Requires the 'view_issues' permission. Skipped occurrences (EXDATEs) are
    excluded so the preview matches what generation would produce.
    """
    async with _get_session_and_user() as (session, user):
        return await _list_recurrence_occurrences(session, user, project_key, pattern_id, days)


@mcp.tool()
async def specivo_list_sprints(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
) -> str:
    """List sprints/iterations for a project with status and dates.

    Use sprint IDs with specivo_update_sprint, specivo_start_sprint, etc.
    """
    async with _get_session_and_user() as (session, user):
        return await _list_sprints(session, user, project_key)


@mcp.tool()
async def specivo_list_sprint_issues(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    sprint_id: Annotated[
        int,
        Field(description="Sprint ID from list_sprints. Use 0 for backlog (unassigned)."),
    ],
    fields: Annotated[
        str,
        Field(
            description=(
                "Field level: 'minimal' (key+subject), "
                "'default' (+status/tracker/priority/assignee), "
                "'full' (+description/done/metadata)."
            ),
        ),
    ] = "default",
    offset: Annotated[int, Field(description="Number of issues to skip (pagination).")] = 0,
    limit: Annotated[int, Field(description="Max issues to return (1-100).")] = 25,
) -> str:
    """List issues in a specific sprint, or backlog issues (sprint_id=0).

    Field levels control output verbosity to save tokens:
    - minimal: display_key and subject only — cheapest for LLMs that just need IDs
    - default: adds status, tracker, priority, assigned_to — like list_issues
    - full: adds description (first 200 chars), done_ratio, sprint_id, version_id, metadata
    """
    async with _get_session_and_user() as (session, user):
        return await _list_sprint_issues(session, user, project_key, sprint_id, fields, offset, limit)


@mcp.tool()
async def specivo_create_sprint(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    name: Annotated[str, Field(description="Sprint name, e.g. 'Sprint 5 — Route Optimization'.")],
    goal: Annotated[str | None, Field(description="Sprint goal text.")] = None,
    start_date: Annotated[str | None, Field(description="Start date in YYYY-MM-DD format.")] = None,
    end_date: Annotated[str | None, Field(description="End date in YYYY-MM-DD format.")] = None,
) -> str:
    """Create a new sprint in a project. Status starts as 'planned'."""
    from datetime import date

    parsed_start: date | None = date.fromisoformat(start_date) if start_date else None
    parsed_end: date | None = date.fromisoformat(end_date) if end_date else None

    async with _get_session_and_user() as (session, user):
        return await _create_sprint(session, user, project_key, name, goal, parsed_start, parsed_end)


@mcp.tool()
async def specivo_update_sprint(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    sprint_id: Annotated[int, Field(description="Sprint ID from specivo_list_sprints.")],
    name: Annotated[str | None, Field(description="New sprint name.")] = None,
    goal: Annotated[str | None, Field(description="New sprint goal.")] = None,
    start_date: Annotated[str | None, Field(description="Start date in YYYY-MM-DD format.")] = None,
    end_date: Annotated[str | None, Field(description="End date in YYYY-MM-DD format.")] = None,
) -> str:
    """Update an existing sprint. Only pass fields you want to change."""
    from datetime import date

    parsed_start: date | None = date.fromisoformat(start_date) if start_date else None
    parsed_end: date | None = date.fromisoformat(end_date) if end_date else None

    async with _get_session_and_user() as (session, user):
        return await _update_sprint(session, user, project_key, sprint_id, name, goal, parsed_start, parsed_end)


@mcp.tool()
async def specivo_start_sprint(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    sprint_id: Annotated[int, Field(description="Sprint ID to start.")],
) -> str:
    """Start a planned sprint (transitions to 'active').

    Only one sprint per project can be active at a time.
    Fails if another sprint is already active.
    """
    async with _get_session_and_user() as (session, user):
        return await _start_sprint(session, user, project_key, sprint_id)


@mcp.tool()
async def specivo_complete_sprint(
    project_key: Annotated[str, Field(description="Uppercase project identifier, e.g. ACME.")],
    sprint_id: Annotated[int, Field(description="Sprint ID to complete.")],
    move_incomplete_to_sprint_id: Annotated[
        int | None, Field(description="Sprint ID to move incomplete issues to. Null = move to backlog.")
    ] = None,
) -> str:
    """Complete an active sprint (transitions to 'completed').

    Incomplete issues are moved to the specified sprint, or to the backlog if null.
    Builds a velocity snapshot with total and completed issue counts.
    """
    async with _get_session_and_user() as (session, user):
        return await _complete_sprint(session, user, project_key, sprint_id, move_incomplete_to_sprint_id)


@mcp.tool()
async def specivo_setup_guide(
    format: Annotated[
        str, Field(description="'claude' for CLAUDE.md, 'cursor' for .cursorrules, 'generic' for plain md.")
    ] = "claude",
) -> str:
    """Return the AI agent setup guide for Specivo.

    Generates configuration content with key concepts, tool overview,
    standard workflows, and anti-patterns. Call once, then save to your project.
    """
    return generate_setup_guide(fmt=format, mcp_server=mcp)
