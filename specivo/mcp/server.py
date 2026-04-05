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

from mcp.server.fastmcp import FastMCP

from specivo.core.database import get_session_factory
from specivo.mcp.auth import authenticate_mcp_tool
from specivo.mcp.tools import (
    _add_comment,
    _create_issue,
    _create_version,
    _create_wiki,
    _edit_description,
    _edit_wiki,
    _list_issues,
    _list_lookups,
    _list_members,
    _list_projects,
    _list_versions,
    _list_wiki_pages,
    _log_time,
    _read_wiki,
    _search,
    _show_issue,
    _update_issue,
    _update_version,
)
from specivo.services.agent_session_service import AgentSessionService
from specivo.services.permission_service import clear_role_cache

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "specivo",
    instructions="Specivo — self-hosted platform for project tracking, knowledge base, and AI-safe automation.",
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
                await _agent_session_svc.get_or_create_session(
                    session, api_key.id, user.id, user_agent=None
                )
            except Exception:
                logger.debug("AgentSession tracking failed", exc_info=True)
            yield session, user
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


@mcp.tool()
async def specivo_list_projects(offset: int = 0, limit: int = 25) -> str:
    """List all projects."""
    async with _get_session_and_user() as (session, user):
        return await _list_projects(session, user, offset, limit)


@mcp.tool()
async def specivo_list_issues(
    project_key: str,
    status: str = "open",
    sort: str = "created_at:desc",
    offset: int = 0,
    limit: int = 25,
) -> str:
    """List issues for a project with filtering."""
    async with _get_session_and_user() as (session, user):
        return await _list_issues(session, user, project_key, status, sort, offset, limit)


@mcp.tool()
async def specivo_show_issue(
    issue_ref: str,
    metadata_only: bool = False,
    search: str | None = None,
) -> str:
    """Show issue details.

    metadata_only: Return only metadata fields (no description body) -- saves tokens.
    search: Return only the section of description matching this text.
    """
    async with _get_session_and_user() as (session, user):
        return await _show_issue(session, user, issue_ref, metadata_only, search)


@mcp.tool()
async def specivo_create_issue(
    project_key: str,
    tracker_id: int,
    subject: str,
    description: str = "",
    status_id: int | None = None,
    priority_id: int | None = None,
    assigned_to_id: int | None = None,
) -> str:
    """Create a new issue."""
    async with _get_session_and_user() as (session, user):
        return await _create_issue(
            session, user, project_key, tracker_id, subject, description,
            status_id, priority_id, assigned_to_id,
        )


@mcp.tool()
async def specivo_update_issue(
    issue_ref: str,
    subject: str | None = None,
    description: str | None = None,
    status_id: int | None = None,
    priority_id: int | None = None,
    assigned_to_id: int | None = None,
    notes: str | None = None,
) -> str:
    """Update an issue. Automatically handles lock_version."""
    async with _get_session_and_user() as (session, user):
        return await _update_issue(
            session, user, issue_ref, subject, description,
            status_id, priority_id, assigned_to_id, notes,
        )


@mcp.tool()
async def specivo_edit_description(
    issue_ref: str,
    search_text: str,
    replace_text: str,
) -> str:
    """Search-and-replace in issue description. Token-efficient editing."""
    async with _get_session_and_user() as (session, user):
        return await _edit_description(session, user, issue_ref, search_text, replace_text)


@mcp.tool()
async def specivo_search(
    query: str,
    project_key: str | None = None,
    scope: str = "all",
    limit: int = 10,
) -> str:
    """Search across issues and wiki pages."""
    async with _get_session_and_user() as (session, user):
        return await _search(session, user, query, project_key, scope, limit)


@mcp.tool()
async def specivo_read_wiki(
    project_key: str,
    slug: str,
    metadata_only: bool = False,
) -> str:
    """Read a wiki page."""
    async with _get_session_and_user() as (session, user):
        return await _read_wiki(session, user, project_key, slug, metadata_only)


@mcp.tool()
async def specivo_list_wiki_pages(project_key: str) -> str:
    """List wiki pages for a project."""
    async with _get_session_and_user() as (session, user):
        return await _list_wiki_pages(session, user, project_key)


@mcp.tool()
async def specivo_edit_wiki(
    project_key: str,
    slug: str,
    search_text: str,
    replace_text: str,
) -> str:
    """Search-and-replace in wiki page content."""
    async with _get_session_and_user() as (session, user):
        return await _edit_wiki(session, user, project_key, slug, search_text, replace_text)


@mcp.tool()
async def specivo_add_comment(
    issue_ref: str,
    notes: str,
) -> str:
    """Add a comment to an issue."""
    async with _get_session_and_user() as (session, user):
        return await _add_comment(session, user, issue_ref, notes)


@mcp.tool()
async def specivo_create_wiki(
    project_key: str,
    title: str,
    text: str,
    parent_slug: str | None = None,
) -> str:
    """Create a new wiki page. Slug is auto-derived from the title."""
    async with _get_session_and_user() as (session, user):
        return await _create_wiki(session, user, project_key, title, text, parent_slug)


@mcp.tool()
async def specivo_list_lookups() -> str:
    """List all trackers, statuses, priorities, and time entry activities with IDs.

    Call this before create_issue or log_time to find valid ID values.
    """
    async with _get_session_and_user() as (session, user):
        return await _list_lookups(session, user)


@mcp.tool()
async def specivo_list_members(project_key: str) -> str:
    """List project members with their roles and user IDs."""
    async with _get_session_and_user() as (session, user):
        return await _list_members(session, user, project_key)


@mcp.tool()
async def specivo_log_time(
    project_key: str,
    hours: float,
    activity_id: int,
    issue_ref: str | None = None,
    comments: str | None = None,
    spent_on: str | None = None,
) -> str:
    """Log time against a project (and optionally a specific issue).

    hours: Decimal hours, e.g. 1.5 for 90 minutes.
    activity_id: Use list_lookups to find valid activity IDs.
    spent_on: ISO date YYYY-MM-DD. Defaults to today.
    """
    from datetime import date
    from decimal import Decimal

    parsed_date: date | None = date.fromisoformat(spent_on) if spent_on else None

    async with _get_session_and_user() as (session, user):
        return await _log_time(
            session, user, project_key, Decimal(str(hours)),
            activity_id, issue_ref, comments, parsed_date,
        )


@mcp.tool()
async def specivo_list_versions(project_key: str) -> str:
    """List project versions/milestones with status and due dates."""
    async with _get_session_and_user() as (session, user):
        return await _list_versions(session, user, project_key)


@mcp.tool()
async def specivo_create_version(
    project_key: str,
    name: str,
    description: str | None = None,
    status: str = "open",
    due_date: str | None = None,
) -> str:
    """Create a new version/milestone. status: open, locked, or closed."""
    from datetime import date

    parsed_date: date | None = date.fromisoformat(due_date) if due_date else None

    async with _get_session_and_user() as (session, user):
        return await _create_version(session, user, project_key, name, description, status, parsed_date)


@mcp.tool()
async def specivo_update_version(
    project_key: str,
    version_id: int,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
    due_date: str | None = None,
) -> str:
    """Update an existing version/milestone."""
    from datetime import date

    parsed_date: date | None = date.fromisoformat(due_date) if due_date else None

    async with _get_session_and_user() as (session, user):
        return await _update_version(
            session, user, project_key, version_id, name, description, status, parsed_date,
        )
