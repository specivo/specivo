"""Centralized security audit logging service."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.security_audit import SecurityAuditLog

logger = logging.getLogger(__name__)


class SecurityAuditService:
    """Stateless service for security audit event logging and querying."""

    # ------------------------------------------------------------------
    # Core INSERT
    # ------------------------------------------------------------------

    @staticmethod
    def _audit_enabled() -> bool:
        """Return True if the security_audit_log feature is available.

        When the enterprise plugin is not loaded, all audit writes are
        silently skipped — both batch (via AuditBatchMiddleware) and direct
        INSERT paths.
        """
        try:
            from specivo.core.features import has_feature

            return has_feature("security_audit_log")
        except (RuntimeError, ImportError):
            return False

    async def log_event(
        self,
        session: AsyncSession,
        event_type: str,
        user_id: int | None = None,
        resource_type: str | None = None,
        resource_id: int | None = None,
        project_id: int | None = None,
        permission: str | None = None,
        ip_address: str | None = None,
        request_id: str | None = None,
        user_agent: str | None = None,
        details: dict[str, Any] | None = None,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Persist a single audit log entry.

        If a ``request`` with an ``audit_events`` buffer on its state is provided,
        the event is appended to the buffer for batch INSERT by AuditBatchMiddleware.
        Otherwise, falls back to a direct INSERT (e.g. CLI or Celery contexts).

        When the enterprise plugin is not loaded (``security_audit_log`` feature
        is not registered), all writes are silently skipped.
        """
        event_data = {
            "event_type": event_type,
            "user_id": user_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "project_id": project_id,
            "permission": permission,
            "ip_address": ip_address,
            "request_id": request_id,
            "user_agent": user_agent,
            "details": details or {},
        }

        # Skip all audit writes when enterprise is not loaded.
        if not self._audit_enabled():
            return SecurityAuditLog(**event_data)

        # Batch mode: append to request buffer for later flush.
        # Check for list type specifically to avoid MagicMock false positives.
        if request is not None and isinstance(getattr(request.state, "audit_events", None), list):
            request.state.audit_events.append(event_data)
            # Return an unsaved model instance for callers that inspect the result
            return SecurityAuditLog(**event_data)

        # Direct INSERT fallback (no request context)
        log = SecurityAuditLog(**event_data)
        session.add(log)
        await session.flush()
        return log

    @classmethod
    async def flush_events(cls, session: AsyncSession, events: list[dict[str, Any]]) -> list[SecurityAuditLog]:
        """Batch INSERT a list of event dicts into the database."""
        logs = []
        for event_data in events:
            log = SecurityAuditLog(**event_data)
            session.add(log)
            logs.append(log)
        await session.flush()
        return logs

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    def _extract_request_info(self, request: Request | None) -> dict[str, str | None]:
        """Extract ip_address, request_id, and user_agent from a FastAPI Request."""
        if request is None:
            return {"ip_address": None, "request_id": None, "user_agent": None}
        ip = request.client.host if request.client else None
        request_id = request.headers.get("x-request-id")
        user_agent = request.headers.get("user-agent")
        return {"ip_address": ip, "request_id": request_id, "user_agent": user_agent}

    async def log_access_granted(
        self,
        session: AsyncSession,
        user_id: int,
        request: Request | None = None,
        resource: str | None = None,
        resource_id: int | None = None,
        project_id: int | None = None,
        permission: str | None = None,
    ) -> SecurityAuditLog:
        """Log a successful access grant."""
        info = self._extract_request_info(request)
        details: dict[str, Any] = {}
        if info["request_id"]:
            details["request_id"] = info["request_id"]
        if info["user_agent"]:
            details["user_agent"] = info["user_agent"]
        if resource:
            details["resource"] = resource
        if resource_id is not None:
            details["resource_id"] = resource_id
        return await self.log_event(
            session=session,
            event_type="access_granted",
            user_id=user_id,
            resource_type=resource,
            resource_id=resource_id,
            project_id=project_id,
            permission=permission,
            ip_address=info["ip_address"],
            details=details,
            request=request,
        )

    async def log_access_denied(
        self,
        session: AsyncSession,
        user_id: int,
        request: Request | None = None,
        resource: str | None = None,
        resource_id: int | None = None,
        project_id: int | None = None,
        permission: str | None = None,
    ) -> SecurityAuditLog:
        """Log an access denial."""
        info = self._extract_request_info(request)
        details: dict[str, Any] = {}
        if info["request_id"]:
            details["request_id"] = info["request_id"]
        if info["user_agent"]:
            details["user_agent"] = info["user_agent"]
        if resource:
            details["resource"] = resource
        if resource_id is not None:
            details["resource_id"] = resource_id
        if permission:
            details["permission"] = permission
        return await self.log_event(
            session=session,
            event_type="access_denied",
            user_id=user_id,
            resource_type=resource,
            resource_id=resource_id,
            project_id=project_id,
            permission=permission,
            ip_address=info["ip_address"],
            details=details,
            request=request,
        )

    async def log_auth_failure(
        self,
        session: AsyncSession,
        reason: str,
        ip_address: str | None = None,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> SecurityAuditLog:
        """Log an authentication failure (user_id is NULL)."""
        merged_details: dict[str, Any] = {"reason": reason}
        if details:
            merged_details.update(details)
        return await self.log_event(
            session=session,
            event_type="auth_failure",
            user_id=None,
            ip_address=ip_address,
            request_id=request_id,
            details=merged_details,
        )

    async def log_search_query(
        self,
        session: AsyncSession,
        user_id: int,
        query: str,
        mode: str,
        scope: str | None = None,
        filters: dict[str, Any] | None = None,
        result_count: int = 0,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log a search query with its parameters and result count."""
        info = self._extract_request_info(request)
        details: dict[str, Any] = {
            "query": query,
            "mode": mode,
            "result_count": result_count,
        }
        if scope:
            details["scope"] = scope
        if filters:
            details["filters"] = filters
        return await self.log_event(
            session=session,
            event_type="search_query",
            user_id=user_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            details=details,
        )

    async def log_resource_viewed(
        self,
        session: AsyncSession,
        user_id: int,
        resource: str,
        resource_key: str,
        resource_id: int | None = None,
        project_id: int | None = None,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log a resource view (issue, wiki page)."""
        info = self._extract_request_info(request)
        details: dict[str, Any] = {
            "resource": resource,
            "resource_key": resource_key,
        }
        return await self.log_event(
            session=session,
            event_type="resource_access",
            user_id=user_id,
            resource_type=resource,
            resource_id=resource_id,
            project_id=project_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            details=details,
        )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def list_events(
        self,
        session: AsyncSession,
        event_type: str | None = None,
        user_id: int | None = None,
        project_id: int | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> tuple[list[SecurityAuditLog], int]:
        """Query audit logs with optional filters. Returns (items, total_count)."""
        stmt = select(SecurityAuditLog)
        count_stmt = select(func.count(SecurityAuditLog.id))

        if event_type is not None:
            stmt = stmt.where(SecurityAuditLog.event_type == event_type)
            count_stmt = count_stmt.where(SecurityAuditLog.event_type == event_type)
        if user_id is not None:
            stmt = stmt.where(SecurityAuditLog.user_id == user_id)
            count_stmt = count_stmt.where(SecurityAuditLog.user_id == user_id)
        if project_id is not None:
            stmt = stmt.where(SecurityAuditLog.project_id == project_id)
            count_stmt = count_stmt.where(SecurityAuditLog.project_id == project_id)
        if after is not None:
            stmt = stmt.where(SecurityAuditLog.created_at >= after)
            count_stmt = count_stmt.where(SecurityAuditLog.created_at >= after)
        if before is not None:
            stmt = stmt.where(SecurityAuditLog.created_at < before)
            count_stmt = count_stmt.where(SecurityAuditLog.created_at < before)

        stmt = stmt.order_by(SecurityAuditLog.created_at.desc()).offset(offset).limit(limit)

        result = await session.execute(stmt)
        items = list(result.scalars().all())

        count_result = await session.execute(count_stmt)
        total = count_result.scalar_one()

        return items, total
