"""Admin audit log API — view security audit trail."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.security_audit import AuditLogListResponse, AuditLogOut
from specivo.services.security_audit_service import SecurityAuditService

router = APIRouter(tags=["admin"])
_service = SecurityAuditService()


def _require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Dependency: raise 403 if the current user is not an admin."""
    if not current_user.is_admin:
        raise PermissionDeniedError("Admin access required")
    return current_user


@router.get("/admin/audit-logs", response_model=AuditLogListResponse)
async def list_audit_logs(
    event_type: str | None = Query(None, description="Filter by event type"),
    user_id: int | None = Query(None, description="Filter by user ID"),
    project_id: int | None = Query(None, description="Filter by project ID"),
    after: datetime | None = Query(None, description="Events after this datetime"),
    before: datetime | None = Query(None, description="Events before this datetime"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    limit: int = Query(25, ge=1, le=100, description="Pagination limit"),
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> AuditLogListResponse:
    """List security audit log entries. Admin-only."""
    items, total_count = await _service.list_events(
        session=db,
        event_type=event_type,
        user_id=user_id,
        project_id=project_id,
        after=after,
        before=before,
        offset=offset,
        limit=limit,
    )
    return AuditLogListResponse(
        total_count=total_count,
        offset=offset,
        limit=limit,
        items=[AuditLogOut.model_validate(item) for item in items],
    )
