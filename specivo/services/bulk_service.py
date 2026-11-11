"""Bulk operations service — update/delete multiple issues at once."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError
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
        "original_estimate",
        "remaining_estimate",
        "done_ratio",
        "is_private",
        "metadata",
        "fixed_version_id",
    }
)


class BulkService:
    """Service for bulk issue operations with per-issue error handling."""

    _issue_service = IssueService()

    async def _load_visible(self, session: AsyncSession, issue_id: int, user: User) -> Issue | None:
        """Return issue *issue_id* if *user* may see it, else None.

        An issue the caller cannot see (another project's private issue, or
        one outside the API key's project limit) is reported as not found,
        never by its key, so ids cannot be probed.
        """
        try:
            return await self._issue_service.get_by_id(session, issue_id, user=user)
        except NotFoundError:
            return None

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
            display_key = f"#{issue_id}"
            try:
                # Load issue
                issue = await self._load_visible(session, issue_id, user)
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

                # Apply update via IssueService (handles workflow, journals, etc.).
                # It sets fields one by one and may refuse one late (a version
                # or category of another project): the savepoint undoes what it
                # flushed, and expiring the issue drops what it had not, so a
                # refused issue keeps nothing and the next issue's flush cannot
                # write it without a journal entry.
                try:
                    async with session.begin_nested():
                        issue = await self._issue_service.update(session, issue, data, user)
                except Exception:
                    session.expire(issue)
                    raise

                succeeded.append(
                    BulkResultItem(
                        id=issue_id,
                        key=display_key,
                        success=True,
                    )
                )

            except Exception as exc:  # noqa: BLE001
                # Catch any error (workflow denied, validation, etc.) — record and continue
                code = getattr(exc, "code", "internal_error")
                message = str(exc)
                failed.append(
                    BulkResultItem(
                        id=issue_id,
                        key=display_key,
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

        # An issue with subtasks cannot be deleted, so delete deepest first: in
        # a nested set a subtask's lft is above its parent's, and a selection
        # holding a parent with all its subtasks is deleted whole, in any order.
        lft_by_id = dict(
            (await session.execute(select(Issue.id, Issue.lft).where(Issue.id.in_(issue_ids)))).tuples().all()
        )
        ordered = sorted(issue_ids, key=lambda i: lft_by_id.get(i) or 0, reverse=True)

        for issue_id in ordered:
            try:
                issue = await self._load_visible(session, issue_id, user)
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

                await self._issue_service.delete(session, issue, actor=user)

                succeeded.append(
                    BulkResultItem(
                        id=issue_id,
                        key=display_key,
                        success=True,
                    )
                )

            except Exception as exc:  # noqa: BLE001
                code = getattr(exc, "code", "internal_error")
                message = str(exc)
                display_key_fallback = f"#{issue_id}"
                try:  # noqa: SIM105
                    display_key_fallback = issue.display_key  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001, S110
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

        # Report in the order the caller listed the issues.
        position = {issue_id: n for n, issue_id in enumerate(issue_ids)}
        succeeded.sort(key=lambda item: position[item.id])
        failed.sort(key=lambda item: position[item.id])
        return BulkResult(succeeded=succeeded, failed=failed)
