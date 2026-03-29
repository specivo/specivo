"""Pydantic schemas for security audit logs."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from specivo.schemas.common import PaginatedResponse


class AuditLogOut(BaseModel):
    """Single audit log entry for API responses."""

    id: int
    event_type: str
    user_id: int | None = None
    resource_type: str | None = None
    resource_id: int | None = None
    project_id: int | None = None
    permission: str | None = None
    ip_address: str | None = None
    request_id: str | None = None
    user_agent: str | None = None
    details: dict = {}
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditLogListResponse(PaginatedResponse[AuditLogOut]):
    """Paginated audit log response."""
