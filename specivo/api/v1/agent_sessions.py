"""Agent session endpoints: a project's sessions, one session, and its tool calls from the audit log.

The list needs access to the project; a session's detail and its tool calls
need ``manage_project`` there (or an administrator), because the audit rows can
name issues and wiki pages across the account's work.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.agent_session import AgentSession
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.agent_session import AgentSessionDetailOut, AgentSessionOut
from specivo.schemas.common import PaginatedResponse
from specivo.schemas.security_audit import AuditLogOut
from specivo.services.agent_session_service import AgentSessionService
from specivo.services.agent_session_usage import session_entries
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import ProjectService
from specivo.services.security_audit_service import SecurityAuditService

router = APIRouter(prefix="/projects/{project_key}/agent-sessions", tags=["agent-sessions"])
_project_service = ProjectService()
_session_service = AgentSessionService()
_audit = SecurityAuditService()

#: Tool calls per page of a session's events, by default.
EVENTS_PAGE_LIMIT = 50


async def _readable_project(db: AsyncSession, project_key: str, user: User) -> Project:
    project = await _project_service.get_by_key(db, project_key)
    await _project_service.require_project_access(db, project, user)
    return project


async def _managed_session(
    db: AsyncSession, project_key: str, session_id: int, user: User, request: Request
) -> tuple[Project, AgentSession]:
    """The project and its session *session_id*, for a caller with ``manage_project`` there.

    403 (audited) without the permission, checked before the session is
    looked up so session ids cannot be probed; 404 for a session that is not
    one of the project's sessions the caller may see.
    """
    project = await _readable_project(db, project_key, user)
    if not await check_permission(user, project.id, Permission.MANAGE_PROJECT, db, request=request):
        raise PermissionDeniedError("You do not have permission to inspect agent sessions in this project")
    agent_session = await _session_service.get_for_project(db, project.id, session_id, user)
    return project, agent_session


@router.get("/", response_model=PaginatedResponse[AgentSessionOut])
async def list_agent_sessions(
    project_key: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    account: str | None = Query(None, max_length=100, description="Only the sessions of the account with this login."),
    issue: str | None = Query(None, max_length=50, description="Only the sessions working on this issue (key)."),
    active: bool | None = Query(
        None, description="true: only open sessions (active within the session timeout); false: only the others."
    ),
    external_session_id: str | None = Query(
        None, max_length=200, description="Only the sessions with this client session id."
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> PaginatedResponse[AgentSessionOut]:
    """List a project's agent sessions, most recently active first.

    Sessions tied to an issue the caller cannot see are left out.
    """
    project = await _readable_project(db, project_key, current_user)
    filters = await _session_service.filters_for(
        db,
        project.id,
        current_user,
        account=account,
        issue_ref=issue,
        active=active,
        external_session_id=external_session_id,
    )
    sessions: list[AgentSession] = []
    total = 0
    if filters is not None:
        sessions, total = await _session_service.list_for_project(
            db, project.id, current_user, filters, offset=offset, limit=limit
        )
    return PaginatedResponse[AgentSessionOut](
        total_count=total,
        offset=offset,
        limit=limit,
        items=[AgentSessionOut.model_validate(s) for s in sessions],
    )


@router.get("/{session_id}/", response_model=AgentSessionDetailOut)
async def get_agent_session(
    project_key: str,
    session_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> AgentSessionDetailOut:
    """One agent session of the project, with its tool call count and AI usage entry count. Needs manage_project."""
    project, agent_session = await _managed_session(db, project_key, session_id, current_user, request)
    _entries, usage_total = await session_entries(db, current_user, agent_session, limit=1)
    await _audit.log_resource_viewed(
        db,
        user_id=current_user.id,
        resource="agent_session",
        resource_key=str(agent_session.id),
        resource_id=agent_session.id,
        project_id=project.id,
        request=request,
    )
    return AgentSessionDetailOut.model_validate(agent_session).model_copy(update={"usage_entry_count": usage_total})


@router.get("/{session_id}/events/", response_model=PaginatedResponse[AuditLogOut])
async def list_agent_session_events(
    project_key: str,
    session_id: int,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(EVENTS_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> PaginatedResponse[AuditLogOut]:
    """The session's MCP tool calls from the security audit log, newest first. Needs manage_project.

    Rows in this project and rows that name no project are listed; the
    session's calls in other projects are not.
    """
    project, agent_session = await _managed_session(db, project_key, session_id, current_user, request)
    items, total = await _audit.list_events(
        db,
        agent_session_id=agent_session.id,
        project_id=project.id,
        include_projectless=True,
        offset=offset,
        limit=limit,
    )
    logins, keys = await _audit.names_for(db, items)
    return PaginatedResponse[AuditLogOut](
        total_count=total,
        offset=offset,
        limit=limit,
        items=[
            AuditLogOut.model_validate(item).model_copy(
                update={
                    "user_login": logins.get(item.user_id) if item.user_id is not None else None,
                    "project_key": keys.get(item.project_id) if item.project_id is not None else None,
                }
            )
            for item in items
        ],
    )
