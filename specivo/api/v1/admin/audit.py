"""Admin audit log API: read the security audit trail.

Read-only administration, so an admin API key may use it (ADR-0004 §7); a
project-limited key is refused by ``require_admin_api``, as it is no global admin.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import ValidationError
from specivo.models.user import User
from specivo.schemas.security_audit import AuditLogListResponse, AuditLogOut
from specivo.services.security_audit_service import SecurityAuditService, is_audit_event

router = APIRouter(tags=["admin"])
_service = SecurityAuditService()


@router.get("/admin/audit-logs/", response_model=AuditLogListResponse)
async def list_audit_logs(
    event_type: str | None = Query(None, description="Filter by one event type"),
    event_types: list[str] | None = Query(
        None, description="Filter by several event types (repeat the parameter); combines with event_type"
    ),
    user_id: int | None = Query(None, description="Filter by user ID"),
    login: str | None = Query(None, description="Filter by the login of an existing user"),
    project_id: int | None = Query(None, description="Filter by project ID"),
    ip_address: str | None = Query(None, max_length=45, description="Filter by client IP address"),
    after: datetime | None = Query(None, description="Events at or after this datetime"),
    before: datetime | None = Query(None, description="Events before this datetime"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    limit: int = Query(25, ge=1, le=100, description="Pagination limit"),
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = DB_SESSION,
) -> AuditLogListResponse:
    """List security audit log entries, newest first. Admin-only.

    An unknown event type or login is refused with 422 rather than answered
    with an empty page, so a typo cannot pass for "nothing happened".
    """
    for value in [event_type, *(event_types or [])]:
        if value is not None and not is_audit_event(value):
            raise ValidationError(f"Unknown event type: {value!r}", field="event_type")
    if login is not None:
        found = await _service.user_id_for_login(db, login)
        if found is None:
            raise ValidationError(f"No user with the login {login!r}", field="login")
        if user_id is not None and user_id != found:
            raise ValidationError("login and user_id name different users", field="login")
        user_id = found

    items, total_count = await _service.list_events(
        session=db,
        event_type=event_type,
        event_types=event_types,
        user_id=user_id,
        project_id=project_id,
        ip_address=ip_address,
        after=after,
        before=before,
        offset=offset,
        limit=limit,
    )
    logins, keys = await _service.names_for(db, items)
    return AuditLogListResponse(
        total_count=total_count,
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
