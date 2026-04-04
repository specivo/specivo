"""Agent session listing endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.agent_session import AgentSessionOut
from specivo.services.agent_session_service import AgentSessionService
from specivo.services.project_service import ProjectService

router = APIRouter(prefix="/projects/{project_key}/agent-sessions", tags=["agent-sessions"])
_project_service = ProjectService()
_session_service = AgentSessionService()


@router.get("/", response_model=list[AgentSessionOut])
async def list_agent_sessions(
    project_key: str,
    limit: int = Query(25, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AgentSessionOut]:
    """List recent agent sessions for a project."""
    project = await _project_service.get_by_key(db, project_key)
    sessions = await _session_service.list_for_project(db, project.id, limit=limit)
    return [AgentSessionOut.model_validate(s) for s in sessions]
