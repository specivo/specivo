"""FastMCP server — exposes Specivo tools over MCP protocol.

Authentication: resolved from the ``SPECIVO_MCP_API_KEY`` environment variable.
Each tool call opens a DB session, authenticates the configured API key,
and delegates to the internal ``_*`` functions in ``tools.py``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from specivo.core.database import get_session_factory
from specivo.mcp.tools import (
    _add_comment,
    _create_issue,
    _edit_description,
    _edit_wiki,
    _list_issues,
    _list_projects,
    _list_wiki_pages,
    _read_wiki,
    _search,
    _show_issue,
    _update_issue,
)
from specivo.services.api_key_service import ApiKeyService

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "specivo",
    instructions="Specivo — self-hosted platform for project tracking, knowledge base, and AI-safe automation.",
)

_api_key_svc = ApiKeyService()


@asynccontextmanager
async def _get_session_and_user() -> AsyncGenerator[tuple, None]:
    """Open a DB session and resolve the MCP user from the configured API key.

    The session is committed on clean exit and rolled back on error.
    """
    raw_key = os.environ.get("SPECIVO_MCP_API_KEY", "")
    if not raw_key:
        raise RuntimeError(
            "SPECIVO_MCP_API_KEY environment variable is not set. "
            "The MCP server requires an API key for authentication."
        )
    factory = get_session_factory()
    async with factory() as session:
        try:
            user, _key = await _api_key_svc.authenticate(session, raw_key)
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
            session,
            user,
            project_key,
            tracker_id,
            subject,
            description,
            status_id,
            priority_id,
            assigned_to_id,
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
            session,
            user,
            issue_ref,
            subject,
            description,
            status_id,
            priority_id,
            assigned_to_id,
            notes,
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
