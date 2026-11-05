"""Pydantic schemas for security audit logs."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from specivo.schemas.common import PaginatedResponse


class AuditLogOut(BaseModel):
    """Single audit log entry for API responses.

    ``user_login`` and ``project_key`` name the user and project the row
    refers to; they are ``None`` when the row names none, or when the user or
    project has been deleted since (the ids are not foreign keys).
    """

    id: int
    event_type: str
    user_id: int | None = None
    user_login: str | None = None
    resource_type: str | None = None
    resource_id: int | None = None
    project_id: int | None = None
    project_key: str | None = None
    permission: str | None = None
    ip_address: str | None = None
    request_id: str | None = None
    user_agent: str | None = None
    details: dict = {}
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditLogListResponse(PaginatedResponse[AuditLogOut]):
    """Paginated audit log response."""
