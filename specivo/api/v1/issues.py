"""Issues API — CRUD, filtering, ?include= support."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.core.database import get_db
from specivo.core.exceptions import PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.user import User
from specivo.schemas.attachment import AttachmentOut
from specivo.schemas.bulk import BulkDeleteRequest, BulkResult, BulkUpdateRequest
from specivo.schemas.common import IdName
from specivo.schemas.issue import (
    IssueCreate,
    IssueListResponse,
    IssueOut,
    IssueUpdate,
    IssueWithChildren,
)
from specivo.schemas.journal import AddCommentRequest, JournalDetailOut, JournalOut, ResolveThreadRequest
from specivo.schemas.watcher import WatcherOut
from specivo.schemas.workflow import AllowedStatusesOut
from specivo.services.attachment_service import AttachmentService
from specivo.services.bulk_service import BulkService
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from specivo.services.mention_service import MentionService
from specivo.services.notification_service import NotificationService
from specivo.services.permission_service import check_permission
from specivo.services.project_service import ProjectService
from specivo.services.saved_filter_service import SavedFilterService
from specivo.services.watcher_service import WatcherService
from specivo.services.workflow_service import WorkflowService

router = APIRouter(tags=["issues"])
_service = IssueService()
_bulk_service = BulkService()
_project_service = ProjectService()
_journal_service = JournalService()
_watcher_service = WatcherService()
_attachment_service = AttachmentService()
_saved_filter_service = SavedFilterService()
_workflow_service = WorkflowService()
_notification_service = NotificationService()
_mention_service = MentionService()


# ---------------------------------------------------------------------------
# Response helper
# ---------------------------------------------------------------------------


def _issue_out(issue: Issue) -> IssueOut:
    """Build an IssueOut from an Issue with eagerly-loaded relationships.

    All relationship attributes (tracker, status, priority, author,
    assigned_to, category) must already be loaded via selectinload before
    calling this helper.
    """
    return IssueOut(
        id=issue.id,
        key=issue.display_key,
        project_key=issue.project_key,
        subject=issue.subject,
        description=issue.description,
        tracker=IdName(id=issue.tracker_id, name=issue.tracker.name),
        status=IdName(id=issue.status_id, name=issue.status.name),
        priority=IdName(id=issue.priority_id, name=issue.priority.name),
        author=IdName(id=issue.author_id, name=issue.author.display_name),
        assigned_to=(
            IdName(id=issue.assigned_to_id, name=issue.assigned_to.display_name)
            if issue.assigned_to_id is not None
            else None
        ),
        category=(IdName(id=issue.category_id, name=issue.category.name) if issue.category_id is not None else None),
        parent_id=issue.parent_id,
        root_id=issue.root_id,
        lft=issue.lft,
        rgt=issue.rgt,
        done_ratio=issue.done_ratio,
        start_date=issue.start_date,
        due_date=issue.due_date,
        estimated_hours=issue.estimated_hours,
        metadata=issue.issue_metadata,
        is_private=issue.is_private,
        lock_version=issue.lock_version,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
    )


# ---------------------------------------------------------------------------
# Project-scoped endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_key}/issues",
    response_model=IssueOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_issue(
    project_key: str,
    data: IssueCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IssueOut:
    """Create a new issue in the given project."""
    project = await _project_service.get_by_key(db, project_key.upper())
    if not await check_permission(current_user, project.id, "add_issues", db, request=request):
        raise PermissionDeniedError("You do not have permission to create issues in this project")
    # Override project_key in body to match path param
    data = data.model_copy(update={"project_key": project.key})
    issue = await _service.create(db, project, data, current_user)
    # Reload with relationships for response
    issue = await _service.get_with_relations(db, issue.id)
    return _issue_out(issue)


@router.get(
    "/projects/{project_key}/issues",
    response_model=IssueListResponse,
)
async def list_issues(
    project_key: str,
    status_filter: str | None = Query(default="open", alias="status"),
    tracker_id: int | None = Query(default=None),
    assigned_to_id: str | None = Query(default=None),
    priority_id: int | None = Query(default=None),
    category_id: int | None = Query(default=None),
    author_id: int | None = Query(default=None),
    subject_contains: str | None = Query(default=None),
    created_after: str | None = Query(default=None),
    created_before: str | None = Query(default=None),
    updated_after: str | None = Query(default=None),
    updated_before: str | None = Query(default=None),
    sort: str = Query(default="created_at:desc"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=200),
    saved_filter_id: int | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IssueListResponse:
    """List issues for a project with optional filtering, sorting, and pagination."""
    project = await _project_service.get_by_key(db, project_key.upper())

    # If a saved filter is specified, load it and use its definition as defaults
    if saved_filter_id is not None:
        sf = await _saved_filter_service.get_by_id(db, saved_filter_id)
        fd = sf.filter_definition
        if status_filter == "open" and "status" in fd:
            status_filter = fd["status"]
        if tracker_id is None and "tracker_id" in fd:
            tracker_id = fd["tracker_id"]
        if assigned_to_id is None and "assigned_to_id" in fd:
            assigned_to_id = str(fd["assigned_to_id"])
        if priority_id is None and "priority_id" in fd:
            priority_id = fd["priority_id"]
        if category_id is None and "category_id" in fd:
            category_id = fd["category_id"]
        if author_id is None and "author_id" in fd:
            author_id = fd["author_id"]
        if subject_contains is None and "subject_contains" in fd:
            subject_contains = fd["subject_contains"]
        if created_after is None and "created_after" in fd:
            created_after = fd["created_after"]
        if created_before is None and "created_before" in fd:
            created_before = fd["created_before"]
        if updated_after is None and "updated_after" in fd:
            updated_after = fd["updated_after"]
        if updated_before is None and "updated_before" in fd:
            updated_before = fd["updated_before"]
        if sort == "created_at:desc" and "sort" in fd:
            sort = fd["sort"]

    # Resolve "me" shorthand for assigned_to_id
    resolved_assigned_to: int | None = None
    if assigned_to_id is not None:
        if assigned_to_id == "me":
            resolved_assigned_to = current_user.id
        else:
            try:
                resolved_assigned_to = int(assigned_to_id)
            except ValueError:
                resolved_assigned_to = None

    filters: dict = {
        "status": status_filter,
        "tracker_id": tracker_id,
        "assigned_to_id": resolved_assigned_to,
        "priority_id": priority_id,
        "category_id": category_id,
        "author_id": author_id,
        "subject_contains": subject_contains,
        "created_after": created_after,
        "created_before": created_before,
        "updated_after": updated_after,
        "updated_before": updated_before,
    }

    issues, total_count = await _service.list_issues(
        session=db,
        project_id=project.id,
        filters=filters,
        sort=sort,
        offset=offset,
        limit=limit,
        user=current_user,
    )

    return IssueListResponse(
        total_count=total_count,
        offset=offset,
        limit=limit,
        items=[_issue_out(i) for i in issues],
    )


# ---------------------------------------------------------------------------
# Bulk operations (must be before {issue_ref} routes to avoid path conflicts)
# ---------------------------------------------------------------------------


@router.post("/issues/bulk-update", response_model=BulkResult)
async def bulk_update_issues(
    data: BulkUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BulkResult:
    """Bulk update multiple issues. Returns per-issue success/failure."""
    return await _bulk_service.bulk_update(db, data.issue_ids, data.updates, current_user)


@router.post("/issues/bulk-delete", response_model=BulkResult)
async def bulk_delete_issues(
    data: BulkDeleteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BulkResult:
    """Bulk delete multiple issues. Returns per-issue success/failure."""
    return await _bulk_service.bulk_delete(db, data.issue_ids, current_user)


# ---------------------------------------------------------------------------
# Global issue endpoints (by display key or numeric ID)
# ---------------------------------------------------------------------------


@router.get("/issues/{issue_ref}", response_model=IssueWithChildren)
async def get_issue(
    issue_ref: str,
    request: Request,
    include: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IssueWithChildren:
    """Get an issue by display key (e.g. ACME-42) or internal numeric ID.

    Supports ``?include=`` with a comma-separated list of:
    - ``children`` — direct child issues (nested set)
    - ``journals`` — activity stream entries (field changes + comments)
    - ``watchers`` — users subscribed to the issue
    - ``attachments`` — file attachments

    Unknown include values are silently ignored.
    """
    issue = await _service.get_by_display_key_with_relations(db, issue_ref, user=current_user)
    out = _issue_out(issue)

    # Audit log the resource view
    try:
        from specivo.services.security_audit_service import SecurityAuditService

        _audit_service = SecurityAuditService()
        await _audit_service.log_resource_viewed(
            session=db,
            user_id=current_user.id,
            resource="issue",
            resource_key=issue.display_key,
            resource_id=issue.id,
            project_id=issue.project_id,
            request=request,
        )
    except Exception:
        import logging

        logging.getLogger(__name__).warning("Failed to log resource view audit", exc_info=True)

    children: list[IssueOut] = []
    journals_out: list[JournalOut] | None = None
    watchers_out: list[WatcherOut] | None = None
    attachments_out: list[AttachmentOut] | None = None

    # Parse ?include= comma-separated list
    include_set: set[str] = set()
    if include:
        include_set = {v.strip() for v in include.split(",")}

    if "children" in include_set:
        # Direct children only: parent_id == issue.id (not all descendants)
        result = await db.execute(
            select(Issue)
            .where(Issue.parent_id == issue.id)
            .order_by(Issue.lft)
            .options(
                selectinload(Issue.tracker),
                selectinload(Issue.status),
                selectinload(Issue.priority),
                selectinload(Issue.category),
                selectinload(Issue.author),
                selectinload(Issue.assigned_to),
            )
        )
        child_issues = list(result.scalars().all())
        children = [_issue_out(c) for c in child_issues]

    if "journals" in include_set:
        include_private = current_user.is_admin
        raw_journals = await _journal_service.list_for_issue(db, issue.id, include_private=include_private)
        # Collect resolved_by user IDs and load them in batch
        resolved_by_ids = {j.resolved_by_id for j in raw_journals if j.resolved_by_id is not None}
        resolved_by_map: dict[int, User] = {}
        if resolved_by_ids:
            user_result = await db.execute(select(User).where(User.id.in_(resolved_by_ids)))
            for u in user_result.scalars().all():
                resolved_by_map[u.id] = u
        journals_out = [
            _journal_out(j, resolved_by_user=resolved_by_map.get(j.resolved_by_id) if j.resolved_by_id else None)
            for j in raw_journals
        ]

    if "watchers" in include_set:
        watcher_users = await _watcher_service.list_watchers(db, issue)
        watchers_out = [
            WatcherOut(
                id=u.id,
                login=u.login,
                display_name=u.display_name,
                email=u.email,
            )
            for u in watcher_users
        ]

    if "attachments" in include_set:
        raw_attachments = await _attachment_service.list_for_container(db, "Issue", issue.id)
        attachments_out = [
            AttachmentOut(
                id=a.id,
                container_type=a.container_type,
                container_id=a.container_id,
                filename=a.filename,
                disk_filename=a.disk_filename,
                content_type=a.content_type,
                filesize=a.filesize,
                description=a.description,
                author=IdName(id=a.author_id, name=a.author.display_name),
                created_at=a.created_at,
                updated_at=a.updated_at,
            )
            for a in raw_attachments
        ]

    return IssueWithChildren(
        **out.model_dump(),
        children=children,
        journals=journals_out,
        watchers=watchers_out,
        attachments=attachments_out,
    )


@router.patch("/issues/{issue_ref}", response_model=IssueOut)
async def update_issue(
    issue_ref: str,
    data: IssueUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IssueOut:
    """Partial update of an issue.

    ``lock_version`` in the request body must match the current DB value
    or the request is rejected with 409 Conflict.
    """
    issue = await _service.get_by_display_key(db, issue_ref, user=current_user)
    if not await check_permission(current_user, issue.project_id, "edit_issues", db):
        raise PermissionDeniedError("You do not have permission to edit issues in this project")
    # Check set_issues_private permission if is_private is being changed
    if data.is_private is not None and data.is_private != issue.is_private:
        if not await check_permission(current_user, issue.project_id, "set_issues_private", db):
            raise PermissionDeniedError("You do not have permission to change issue privacy")
    old_assignee_id = issue.assigned_to_id
    issue = await _service.update(db, issue, data, current_user)

    # --- Notifications (from API layer, keeping services decoupled) ---
    if data.assigned_to_id is not None and data.assigned_to_id != old_assignee_id:
        await _notification_service.notify_assignment(
            session=db,
            issue=issue,
            old_assignee_id=old_assignee_id,
            new_assignee_id=data.assigned_to_id,
            actor=current_user,
        )
    if data.status_id is not None:
        await _notification_service.notify_watchers(
            session=db,
            issue=issue,
            event_type="status_change",
            actor=current_user,
        )

    # Reload with relationships
    issue = await _service.get_with_relations(db, issue.id)
    return _issue_out(issue)


@router.delete("/issues/{issue_ref}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_issue(
    issue_ref: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an issue permanently."""
    issue = await _service.get_by_display_key(db, issue_ref, user=current_user)
    if not await check_permission(current_user, issue.project_id, "delete_issues", db):
        raise PermissionDeniedError("You do not have permission to delete issues in this project")
    await _service.delete(db, issue)


# ---------------------------------------------------------------------------
# Allowed statuses (workflow)
# ---------------------------------------------------------------------------


@router.get(
    "/issues/{issue_ref}/allowed-statuses",
    response_model=AllowedStatusesOut,
    tags=["workflow"],
)
async def get_allowed_statuses(
    issue_ref: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AllowedStatusesOut:
    """Return the list of status IDs the current user can transition this issue to."""
    issue = await _service.get_by_display_key(db, issue_ref, user=current_user)

    if current_user.is_admin:
        # Admin can transition to any status — return all status IDs
        from specivo.models.lookups import IssueStatus as IssueStatusModel

        result = await db.execute(select(IssueStatusModel.id).order_by(IssueStatusModel.position))
        all_ids = [row[0] for row in result.all()]
        return AllowedStatusesOut(allowed_status_ids=all_ids)

    role_ids = await _workflow_service._get_user_role_ids(db, current_user, issue.project_id)
    if not role_ids:
        return AllowedStatusesOut(allowed_status_ids=[])

    # Check if any transitions exist at all
    has_rules = await _workflow_service._has_any_transitions(db)
    if not has_rules:
        # No workflow rules — return all statuses (backward compat)
        from specivo.models.lookups import IssueStatus as IssueStatusModel

        result = await db.execute(select(IssueStatusModel.id).order_by(IssueStatusModel.position))
        all_ids = [row[0] for row in result.all()]
        return AllowedStatusesOut(allowed_status_ids=all_ids)

    allowed = await _workflow_service.get_allowed_statuses(db, issue.tracker_id, role_ids, issue.status_id)
    return AllowedStatusesOut(allowed_status_ids=allowed)


# ---------------------------------------------------------------------------
# Journal helpers
# ---------------------------------------------------------------------------


def _journal_out(j: Journal, resolved_by_user: User | None = None) -> JournalOut:
    """Build a JournalOut from a Journal with eagerly-loaded relationships."""
    resolved_by = None
    if j.resolved_by_id is not None and resolved_by_user is not None:
        resolved_by = IdName(id=resolved_by_user.id, name=resolved_by_user.display_name)
    return JournalOut(
        id=j.id,
        sequence=j.sequence,
        issue_id=j.issue_id,
        user=IdName(id=j.user_id, name=j.user.display_name),
        notes=j.notes,
        is_private=j.is_private,
        details=[
            JournalDetailOut(
                id=d.id,
                property=d.property,
                prop_key=d.prop_key,
                old_value=d.old_value,
                new_value=d.new_value,
            )
            for d in j.details
        ],
        reply_to_id=j.reply_to_id,
        is_resolved=j.is_resolved,
        resolved_by=resolved_by,
        resolved_at=j.resolved_at,
        resolved_summary=j.resolved_summary,
        created_at=j.created_at,
        updated_at=j.updated_at,
    )


# ---------------------------------------------------------------------------
# Journal endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/issues/{issue_ref}/journals",
    response_model=JournalOut,
    status_code=status.HTTP_201_CREATED,
    tags=["journals"],
)
async def add_comment(
    issue_ref: str,
    data: AddCommentRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JournalOut:
    """Add a comment to an issue."""
    issue = await _service.get_by_display_key(db, issue_ref, user=current_user)
    if not await check_permission(current_user, issue.project_id, "add_issue_notes", db):
        raise PermissionDeniedError("You do not have permission to comment on issues in this project")

    api_key_id: int | None = None

    journal = await _journal_service.add_comment(
        session=db,
        issue=issue,
        user=current_user,
        notes=data.notes,
        api_key_id=api_key_id,
        reply_to_id=data.reply_to_id,
    )
    # Reload with user relationship (journal was just flushed, needs full load)
    result = await db.execute(
        select(Journal)
        .where(Journal.id == journal.id)
        .options(selectinload(Journal.user), selectinload(Journal.details))
    )
    journal = result.scalar_one()

    # Notify watchers about the new comment
    await _notification_service.notify_comment(
        session=db,
        issue=issue,
        journal=journal,
        actor=current_user,
    )

    # Process @mentions in the comment text
    await _mention_service.process_mentions(
        session=db,
        journal=journal,
        text=data.notes,
        actor=current_user,
    )

    return _journal_out(journal)


@router.post(
    "/issues/{issue_ref}/journals/{journal_id}/resolve",
    response_model=JournalOut,
    tags=["journals"],
)
async def resolve_thread(
    issue_ref: str,
    journal_id: int,
    data: ResolveThreadRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JournalOut:
    """Mark a journal thread as resolved."""
    issue = await _service.get_by_display_key(db, issue_ref, user=current_user)
    if not await check_permission(current_user, issue.project_id, "add_issue_notes", db):
        raise PermissionDeniedError("You do not have permission to manage threads in this project")

    journal = await _journal_service.resolve_thread(
        session=db,
        journal_id=journal_id,
        issue_id=issue.id,
        user=current_user,
        summary=data.summary,
    )
    # Reload with details
    result = await db.execute(
        select(Journal)
        .where(Journal.id == journal.id)
        .options(selectinload(Journal.user), selectinload(Journal.details))
    )
    journal = result.scalar_one()

    return _journal_out(journal, resolved_by_user=current_user)


@router.post(
    "/issues/{issue_ref}/journals/{journal_id}/unresolve",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["journals"],
)
async def unresolve_thread(
    issue_ref: str,
    journal_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Clear resolution on a journal thread."""
    issue = await _service.get_by_display_key(db, issue_ref, user=current_user)
    if not await check_permission(current_user, issue.project_id, "add_issue_notes", db):
        raise PermissionDeniedError("You do not have permission to manage threads in this project")

    await _journal_service.unresolve_thread(
        session=db,
        journal_id=journal_id,
        issue_id=issue.id,
        user=current_user,
    )


# ---------------------------------------------------------------------------
# Watcher endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/issues/{issue_ref}/watchers",
    status_code=status.HTTP_201_CREATED,
    tags=["watchers"],
)
async def watch_issue(
    issue_ref: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Subscribe the current user to an issue."""
    issue = await _service.get_by_display_key(db, issue_ref, user=current_user)
    await _watcher_service.watch(db, issue, current_user)
    return {"watched": True}


@router.delete(
    "/issues/{issue_ref}/watchers",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["watchers"],
)
async def unwatch_issue(
    issue_ref: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Unsubscribe the current user from an issue."""
    issue = await _service.get_by_display_key(db, issue_ref, user=current_user)
    await _watcher_service.unwatch(db, issue, current_user)


@router.get(
    "/issues/{issue_ref}/watchers",
    response_model=list[WatcherOut],
    tags=["watchers"],
)
async def list_watchers(
    issue_ref: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[WatcherOut]:
    """List users watching an issue."""
    issue = await _service.get_by_display_key(db, issue_ref, user=current_user)
    watcher_users = await _watcher_service.list_watchers(db, issue)
    return [
        WatcherOut(
            id=u.id,
            login=u.login,
            display_name=u.display_name,
            email=u.email,
        )
        for u in watcher_users
    ]
