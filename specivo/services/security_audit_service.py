"""Centralized security audit logging service."""

from __future__ import annotations

import logging
from datetime import datetime
from enum import StrEnum
from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.security_audit import SecurityAuditLog

logger = logging.getLogger(__name__)


class AuditEvent(StrEnum):
    """All valid audit event types stored in SecurityAuditLog.event_type."""

    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    SEARCH_QUERY = "search_query"
    MEMBER_CHANGE = "member_change"
    ACCESS_GRANTED = "access_granted"
    ACCESS_DENIED = "access_denied"
    AUTH_FAILURE = "auth_failure"
    RESOURCE_ACCESS = "resource_access"
    ATTACHMENT_UPLOADED = "attachment_uploaded"
    ATTACHMENT_DELETED = "attachment_deleted"
    ATTACHMENT_UPDATED = "attachment_description_updated"
    PROJECT_KEY_RENAMED = "project_key_renamed"
    ISSUE_CREATED = "issue_created"
    ISSUE_UPDATED = "issue_updated"
    ISSUE_READ = "issue_read"
    ISSUE_LISTED = "issue_listed"
    WIKI_READ = "wiki_read"
    WIKI_UPDATED = "wiki_updated"
    WIKI_LISTED = "wiki_listed"
    COMMENT_ADDED = "comment_added"
    PROJECTS_LISTED = "projects_listed"
    WIKI_CREATED = "wiki_created"
    WIKI_DELETED = "wiki_deleted"
    WIKI_RESTORED = "wiki_restored"
    MEMBERS_LISTED = "members_listed"
    LOOKUPS_READ = "lookups_read"
    TIME_LOGGED = "time_logged"
    VERSIONS_LISTED = "versions_listed"
    VERSION_CREATED = "version_created"
    VERSION_UPDATED = "version_updated"
    VERSION_DELETED = "version_deleted"
    RECURRING_PATTERNS_LISTED = "recurring_patterns_listed"
    RECURRING_PATTERN_CREATED = "recurring_pattern_created"
    RECURRING_PATTERN_UPDATED = "recurring_pattern_updated"
    RECURRING_PATTERN_DELETED = "recurring_pattern_deleted"
    RECURRENCE_OCCURRENCE_SKIPPED = "recurrence_occurrence_skipped"
    RECURRENCE_OCCURRENCES_LISTED = "recurrence_occurrences_listed"
    METADATA_SCHEMA_CREATED = "metadata_schema_created"
    METADATA_SCHEMA_UPDATED = "metadata_schema_updated"
    METADATA_SCHEMA_DELETED = "metadata_schema_deleted"
    RELATION_LISTED = "relation_listed"
    RELATION_ADDED = "relation_added"
    RELATION_REMOVED = "relation_removed"
    PASSWORD_RESET_REQUESTED = "password_reset_requested"
    PASSWORD_RESET_COMPLETED = "password_reset_completed"
    PASSWORD_RESET_FAILED = "password_reset_failed"


class MemberAction(StrEnum):
    """Valid actions for member_change audit events (details.action)."""

    ADDED = "added"
    REMOVED = "removed"
    ROLES_CHANGED = "roles_changed"
    PERMISSION_DENIED = "permission_denied"


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
        event_type: str | AuditEvent,
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
        _valid = {e.value for e in AuditEvent}
        if str(event_type) not in _valid:
            raise ValueError(f"Unknown audit event type: {event_type!r}. Use AuditEvent enum.")

        event_data = {
            "event_type": str(event_type),
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
            event_type=AuditEvent.ACCESS_GRANTED,
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
            event_type=AuditEvent.ACCESS_DENIED,
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
            event_type=AuditEvent.AUTH_FAILURE,
            user_id=None,
            ip_address=ip_address,
            request_id=request_id,
            details=merged_details,
        )

    async def log_login_success(
        self,
        session: AsyncSession,
        user_id: int,
        request: Request | None = None,
        details: dict[str, Any] | None = None,
    ) -> SecurityAuditLog:
        """Log a successful login. Core feature — always persisted."""
        info = self._extract_request_info(request)
        merged: dict[str, Any] = {"method": "password"}
        if details:
            merged.update(details)
        log = SecurityAuditLog(
            event_type=AuditEvent.LOGIN_SUCCESS,
            user_id=user_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=merged,
        )
        session.add(log)
        await session.flush()
        return log

    async def log_login_failure(
        self,
        session: AsyncSession,
        request: Request | None = None,
        login_hint: str | None = None,
        reason: str = "invalid_credentials",
    ) -> SecurityAuditLog:
        """Log a failed login attempt. Core feature — always persisted."""
        info = self._extract_request_info(request)
        details: dict[str, Any] = {"reason": reason}
        if login_hint:
            details["login_hint"] = login_hint
        log = SecurityAuditLog(
            event_type=AuditEvent.LOGIN_FAILURE,
            user_id=None,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )
        session.add(log)
        await session.flush()
        return log

    async def log_member_change(
        self,
        session: AsyncSession,
        action: MemberAction,
        user_id: int,
        project_id: int,
        target_user_id: int,
        target_login: str,
        roles: list[str] | None = None,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log a project member change. Core feature — always persisted."""
        info = self._extract_request_info(request)
        details: dict[str, Any] = {
            "action": str(action),
            "target_user_id": target_user_id,
            "target_login": target_login,
        }
        if roles:
            details["roles"] = roles
        log = SecurityAuditLog(
            event_type=AuditEvent.MEMBER_CHANGE,
            user_id=user_id,
            project_id=project_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )
        session.add(log)
        await session.flush()
        return log

    async def log_search_query(
        self,
        session: AsyncSession,
        user_id: int,
        query: str,
        mode: str,
        scope: str | None = None,
        filters: dict[str, Any] | None = None,
        result_count: int = 0,
        type_counts: dict[str, int] | None = None,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log a search query. Core feature — always persisted."""
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
        if type_counts:
            details["type_counts"] = type_counts
        log = SecurityAuditLog(
            event_type=AuditEvent.SEARCH_QUERY,
            user_id=user_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )
        session.add(log)
        await session.flush()
        return log

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
            event_type=AuditEvent.RESOURCE_ACCESS,
            user_id=user_id,
            resource_type=resource,
            resource_id=resource_id,
            project_id=project_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            details=details,
        )

    async def log_password_reset_requested(
        self,
        session: AsyncSession,
        user_id: int | None,
        request: Request | None = None,
        email_hint: str | None = None,
    ) -> SecurityAuditLog:
        """Log a password reset request. Core feature — always persisted.

        *user_id* is set when the email matched a real account, None otherwise.
        """
        info = self._extract_request_info(request)
        details: dict[str, Any] = {}
        if email_hint:
            details["email_hint"] = email_hint
        log = SecurityAuditLog(
            event_type=AuditEvent.PASSWORD_RESET_REQUESTED,
            user_id=user_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )
        session.add(log)
        await session.flush()
        return log

    async def log_password_reset_completed(
        self,
        session: AsyncSession,
        user_id: int,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log a successful password reset. Core feature — always persisted."""
        info = self._extract_request_info(request)
        log = SecurityAuditLog(
            event_type=AuditEvent.PASSWORD_RESET_COMPLETED,
            user_id=user_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details={},
        )
        session.add(log)
        await session.flush()
        return log

    async def log_password_reset_failed(
        self,
        session: AsyncSession,
        reason: str,
        request: Request | None = None,
        user_id: int | None = None,
    ) -> SecurityAuditLog:
        """Log a failed password reset attempt. Core feature — always persisted."""
        info = self._extract_request_info(request)
        details: dict[str, Any] = {"reason": reason}
        log = SecurityAuditLog(
            event_type=AuditEvent.PASSWORD_RESET_FAILED,
            user_id=user_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )
        session.add(log)
        await session.flush()
        return log

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
