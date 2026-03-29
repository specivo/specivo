"""Bulk operations service — update/delete multiple issues at once."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.user import User
from specivo.schemas.bulk import BulkResult, BulkResultItem
from specivo.schemas.issue import IssueUpdate
from specivo.services.issue_service import IssueService
from specivo.services.permission_service import check_permission

logger = logging.getLogger(__name__)

# Fields from the updates dict that map directly to IssueUpdate fields.
_UPDATABLE_FIELDS = frozenset(
    {
        "tracker_id",
        "status_id",
        "priority_id",
        "subject",
        "description",
        "assigned_to_id",
        "category_id",
        "parent_id",
        "start_date",
        "due_date",
        "estimated_hours",
        "done_ratio",
        "is_private",
        "metadata",
    }
)

# Fields that are applied directly on the Issue model (not via IssueUpdate).
_DIRECT_FIELDS = frozenset(
    {
        "fixed_version_id",
    }
)


class BulkService:
    """Service for bulk issue operations with per-issue error handling."""

    _issue_service = IssueService()

    async def bulk_update(
        self,
        session: AsyncSession,
        issue_ids: list[int],
        updates: dict[str, Any],
        user: User,
    ) -> BulkResult:
        """Update multiple issues. Returns succeeded + failed lists.

        Per-issue: check permissions, workflow validation, create journals.
        Partial failure: some succeed, some fail with error details.
        """
        succeeded: list[BulkResultItem] = []
        failed: list[BulkResultItem] = []

        for issue_id in issue_ids:
            try:
                # Load issue
                result = await session.execute(select(Issue).where(Issue.id == issue_id))
                issue = result.scalar_one_or_none()
                if issue is None:
                    failed.append(
                        BulkResultItem(
                            id=issue_id,
                            key=f"#{issue_id}",
                            success=False,
                            error={"code": "not_found", "message": f"Issue {issue_id} not found"},
                        )
                    )
                    continue

                display_key = issue.display_key

                # Permission check
                if not await check_permission(user, issue.project_id, "edit_issues", session):
                    failed.append(
                        BulkResultItem(
                            id=issue_id,
                            key=display_key,
                            success=False,
                            error={"code": "permission_denied", "message": "No edit permission"},
                        )
                    )
                    continue

                # Build IssueUpdate with current lock_version (auto-handle)
                update_fields: dict[str, Any] = {"lock_version": issue.lock_version}
                for field in _UPDATABLE_FIELDS:
                    if field in updates:
                        update_fields[field] = updates[field]

                data = IssueUpdate(**update_fields)

                # Apply update via IssueService (handles workflow, journals, etc.)
                issue = await self._issue_service.update(session, issue, data, user)

                # Handle direct fields not in IssueUpdate (e.g. fixed_version_id)
                direct_changed = False
                for field in _DIRECT_FIELDS:
                    if field in updates:
                        setattr(issue, field, updates[field])
                        direct_changed = True

                if direct_changed:
                    await session.flush()

                succeeded.append(
                    BulkResultItem(
                        id=issue_id,
                        key=display_key,
                        success=True,
                    )
                )

            except Exception as exc:
                # Catch any error (workflow denied, validation, etc.) — record and continue
                code = getattr(exc, "code", "internal_error")
                message = str(exc)
                display_key_fallback = f"#{issue_id}"
                try:
                    # Try to get display_key if issue was loaded
                    display_key_fallback = issue.display_key  # type: ignore[union-attr,possibly-undefined]
                except Exception:
                    pass

                failed.append(
                    BulkResultItem(
                        id=issue_id,
                        key=display_key_fallback,
                        success=False,
                        error={"code": code, "message": message},
                    )
                )
                logger.debug("Bulk update failed for issue %d: %s", issue_id, message)

        return BulkResult(succeeded=succeeded, failed=failed)

    async def bulk_delete(
        self,
        session: AsyncSession,
        issue_ids: list[int],
        user: User,
    ) -> BulkResult:
        """Delete multiple issues. Permission check per issue."""
        succeeded: list[BulkResultItem] = []
        failed: list[BulkResultItem] = []

        for issue_id in issue_ids:
            try:
                result = await session.execute(select(Issue).where(Issue.id == issue_id))
                issue = result.scalar_one_or_none()
                if issue is None:
                    failed.append(
                        BulkResultItem(
                            id=issue_id,
                            key=f"#{issue_id}",
                            success=False,
                            error={"code": "not_found", "message": f"Issue {issue_id} not found"},
                        )
                    )
                    continue

                display_key = issue.display_key

                # Permission check
                if not await check_permission(user, issue.project_id, "delete_issues", session):
                    failed.append(
                        BulkResultItem(
                            id=issue_id,
                            key=display_key,
                            success=False,
                            error={"code": "permission_denied", "message": "No delete permission"},
                        )
                    )
                    continue

                await self._issue_service.delete(session, issue)

                succeeded.append(
                    BulkResultItem(
                        id=issue_id,
                        key=display_key,
                        success=True,
                    )
                )

            except Exception as exc:
                code = getattr(exc, "code", "internal_error")
                message = str(exc)
                display_key_fallback = f"#{issue_id}"
                try:
                    display_key_fallback = issue.display_key  # type: ignore[union-attr,possibly-undefined]
                except Exception:
                    pass

                failed.append(
                    BulkResultItem(
                        id=issue_id,
                        key=display_key_fallback,
                        success=False,
                        error={"code": code, "message": message},
                    )
                )
                logger.debug("Bulk delete failed for issue %d: %s", issue_id, message)

        return BulkResult(succeeded=succeeded, failed=failed)
