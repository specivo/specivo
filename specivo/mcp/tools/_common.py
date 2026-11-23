"""Shared helpers for the MCP tool implementations: service instances, audit logging, project access.

The ``_*`` tool functions in this package accept an explicit ``session`` and
``user``, delegate to the service layer, and return a formatted string. The
MCP ``@mcp.tool()`` wrappers in ``server.py`` resolve auth and session, then
call them.
"""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError, PermissionDeniedError
from specivo.mcp.client_context import agent_session_id_var
from specivo.models.agent_session import AgentSession
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.entity_link_service import EntityLinkService
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from specivo.services.notification_service import NotificationService
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import ProjectService
from specivo.services.recurring_pattern_service import RecurringPatternService
from specivo.services.relation_service import RelationService
from specivo.services.search_service import SearchService
from specivo.services.security_audit_service import AuditEvent, bounded_log, current_request_info
from specivo.services.sprint_service import SprintService
from specivo.services.tag_service import TagService
from specivo.services.time_entry_service import TimeEntryService
from specivo.services.version_service import VersionService
from specivo.services.wiki_service import WikiService

_issue_svc = IssueService()
_link_svc = EntityLinkService()
_project_svc = ProjectService()
_wiki_svc = WikiService()
_search_svc = SearchService()
_journal_svc = JournalService()
_time_entry_svc = TimeEntryService()
_version_svc = VersionService()
_relation_svc = RelationService()
_sprint_svc = SprintService()
_recurring_pattern_svc = RecurringPatternService()
_tag_svc = TagService()
_notification_svc = NotificationService()


async def _log_tool(
    session: AsyncSession,
    user: User,
    event_type: str | AuditEvent,
    tool_name: str,
    details: dict | None = None,
    project_id: int | None = None,
) -> None:
    """Log an MCP tool call, with where the call came from when the request's audit context reaches it.

    The row names the agent session of the call (``agent_session_id_var``),
    and that session's ``tool_call_count`` goes up by one in the same
    transaction, so a tool call that rolls back is neither logged nor counted.
    """
    log_details = {"tool": tool_name, "source": "mcp"}
    if details:
        log_details.update(details)
    agent_session_id = agent_session_id_var.get()
    log = bounded_log(
        {
            "event_type": str(event_type),
            "user_id": user.id,
            "project_id": project_id,
            "agent_session_id": agent_session_id,
            "details": log_details,
            **current_request_info(),
        }
    )
    session.add(log)
    if agent_session_id is not None:
        await session.execute(
            update(AgentSession)
            .where(AgentSession.id == agent_session_id)
            .values(tool_call_count=AgentSession.tool_call_count + 1)
            .execution_options(synchronize_session=False)
        )
    await session.flush()


async def _get_project(session: AsyncSession, user: User, key: str) -> Project:
    """Resolve project *key* for *user*, or raise ``NotFoundError``.

    A key that names no project and a project *user* cannot reach (private
    without membership, outside the API key's limit) get the same message,
    built from the key as the caller wrote it: echoing the stored key would
    reveal that the project exists, or what a retired key was renamed to.
    """
    try:
        project = await _project_svc.get_by_key(session, key)
        await _project_svc.require_project_access(session, project, user)
    except NotFoundError:
        raise NotFoundError(f"Project '{key}' not found") from None
    return project


async def _require_permission(
    session: AsyncSession,
    user: User,
    project_id: int,
    permission: str | Permission,
) -> None:
    """Raise unless *user* holds *permission* in project *project_id*.

    A project the user cannot reach at all (private without membership, or
    outside the API key's limit) is "not found", so a refusal does not reveal
    which private projects exist (ADR-0004). Only a reachable project answers
    with a permission error.
    """
    project = await session.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project not found")
    try:
        await _project_svc.require_project_access(session, project, user)
    except NotFoundError:
        # Callers resolve the project with _get_project first; this is a
        # backstop, so it names no key at all.
        raise NotFoundError("Project not found") from None
    if not await check_permission(user, project_id, permission, session):
        raise PermissionDeniedError(f"Permission '{permission}' denied for this project")


def _extract_section(text: str, search: str) -> str | None:
    """Extract the paragraph/section containing ``search``.

    Splits on double-newline (paragraph break) or markdown headings
    and returns the matching block plus its heading context.
    """
    if search not in text:
        return None

    # Split into sections by markdown headings or double newlines
    import re

    # Split on lines that start with ## (markdown heading) keeping the delimiter
    parts = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    if len(parts) <= 1:
        # Fall back to paragraph split
        parts = text.split("\n\n")

    for part in parts:
        if search in part:
            return part.strip()

    return None
