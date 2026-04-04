"""Web issue pages: list, detail, create/edit forms."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.version import Version
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from specivo.services.project_service import ProjectService
from specivo.services.reaction_service import ReactionService
from specivo.services.relation_service import RelationService
from specivo.services.saved_filter_service import SavedFilterService
from specivo.web.deps import get_current_user_optional, get_templates
from specivo.web.thread_tree import build_thread_tree

if TYPE_CHECKING:
    from specivo.models.user import User

router = APIRouter(tags=["web-issues"], include_in_schema=False)

_issue_svc = IssueService()
_journal_svc = JournalService()
_project_svc = ProjectService()
_reaction_svc = ReactionService()
_relation_svc = RelationService()
_saved_filter_svc = SavedFilterService()


async def _get_lookups(db: AsyncSession) -> dict:
    """Load trackers, statuses, priorities for dropdown options."""
    trackers = (await db.execute(select(Tracker).order_by(Tracker.position))).scalars().all()
    statuses = (await db.execute(select(IssueStatus).order_by(IssueStatus.position))).scalars().all()
    priorities = (
        (await db.execute(select(IssuePriority).where(IssuePriority.active.is_(True)).order_by(IssuePriority.position)))
        .scalars()
        .all()
    )
    return {
        "trackers": list(trackers),
        "statuses": list(statuses),
        "priorities": list(priorities),
    }


@router.get("/projects/{project_key}/issues/", response_class=HTMLResponse)
async def issues_list(
    project_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    status: str = Query("open"),
    tracker_id: str = Query(""),
    assigned_to_id: str = Query(""),
    priority_id: str = Query(""),
    sort: str = Query("created_at:desc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> Response:
    """Render the issue list page for a project."""
    from specivo.core.utils import safe_int

    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    filters: dict = {"status": status}
    if safe_int(tracker_id) is not None:
        filters["tracker_id"] = safe_int(tracker_id)
    if safe_int(assigned_to_id) is not None:
        filters["assigned_to_id"] = safe_int(assigned_to_id)
    if safe_int(priority_id) is not None:
        filters["priority_id"] = safe_int(priority_id)

    issues, total = await _issue_svc.list_issues(
        db,
        project_id=project.id,
        filters=filters,
        sort=sort,
        offset=offset,
        limit=limit,
        user=user,
    )

    saved_filters = await _saved_filter_svc.list_for_project(db, user, project.id)
    lookups = await _get_lookups(db)

    # Get project members for assignee dropdown
    members = await _project_svc.list_members(db, project)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/list.html",
        context={
            "user": user,
            "active_page": "issues",
            "active_project": project,
            "project": project,
            "issues": issues,
            "total": total,
            "saved_filters": saved_filters,
            "members": members,
            "offset": offset,
            "limit": limit,
            "sort": sort,
            "filters": filters,
            **lookups,
        },
    )


@router.get("/projects/{project_key}/issues/new/", response_class=HTMLResponse)
async def issue_create_form(
    project_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the issue creation form."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    lookups = await _get_lookups(db)
    members = await _project_svc.list_members(db, project)
    versions_result = await db.execute(select(Version).where(Version.project_id == project.id).order_by(Version.name))
    versions = list(versions_result.scalars().all())

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/form.html",
        context={
            "user": user,
            "active_page": "issues",
            "active_project": project,
            "project": project,
            "issue": None,
            "mode": "create",
            "members": members,
            "versions": versions,
            **lookups,
        },
    )


@router.get("/projects/{project_key}/issues/{issue_ref}/", response_class=HTMLResponse)
async def issue_detail(
    project_key: str,
    issue_ref: str,
    request: Request,
    activity_page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the issue detail page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    try:
        issue = await _issue_svc.get_by_display_key_with_relations(db, issue_ref, user=user)
    except NotFoundError:
        return JSONResponse({"detail": "Issue not found"}, status_code=404)

    from specivo.core.constants import ACTIVITY_DEFAULT_PER_PAGE, ACTIVITY_PER_PAGE_OPTIONS

    all_journals = await _journal_svc.list_for_issue(db, issue.id, include_private=user.is_admin)

    # Paginate activity feed
    activity_per_page = user.preferences.get("activity_per_page", ACTIVITY_DEFAULT_PER_PAGE)
    if activity_per_page not in ACTIVITY_PER_PAGE_OPTIONS:
        activity_per_page = ACTIVITY_DEFAULT_PER_PAGE

    activity_total = len(all_journals)
    activity_total_pages = max(1, (activity_total + activity_per_page - 1) // activity_per_page)
    if activity_page > activity_total_pages:
        activity_page = activity_total_pages

    activity_start = (activity_page - 1) * activity_per_page
    journals = all_journals[activity_start : activity_start + activity_per_page]
    thread_tree = build_thread_tree(journals)

    # Latest activity: most recent of issue.updated_at and last journal
    last_activity_at = issue.updated_at
    last_journal_at = all_journals[-1].created_at if all_journals else None
    if last_journal_at and (not last_activity_at or last_journal_at > last_activity_at):
        last_activity_at = last_journal_at

    # Load emoji reactions for all journals (paginated subset)
    journal_ids = [j.id for j in journals]
    reactions_by_journal_raw = await _reaction_svc.list_reactions_bulk(db, journal_ids, user.id)
    reactions_by_journal = {
        jid: [{"emoji": r.emoji, "count": r.count, "reacted_by_me": r.reacted_by_me} for r in groups]
        for jid, groups in reactions_by_journal_raw.items()
    }

    lookups = await _get_lookups(db)
    members = await _project_svc.list_members(db, project)

    # Build lookup maps for human-readable activity details
    lookup_maps: dict[str, dict[str, str]] = {
        "status_id": {str(s.id): s.name for s in lookups["statuses"]},
        "tracker_id": {str(t.id): t.name for t in lookups["trackers"]},
        "priority_id": {str(p.id): p.name for p in lookups["priorities"]},
        "assigned_to_id": {
            str(m.get("user_id", m.get("id", ""))): m.get("login", m.get("display_name", "")) for m in members
        },
    }

    # Tab data (counts, time entries, attachments, activities)
    tab_ctx = await _issue_svc.get_detail_tab_context(db, issue.id)

    # Watchers
    from specivo.services.watcher_service import WatcherService

    watcher_svc = WatcherService()
    watchers = await watcher_svc.list_watchers(db, issue)
    is_watching = await watcher_svc.is_watching(db, issue, user)

    # Relations for the relations tab
    relations = await _relation_svc.list_for_issue(db, issue)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/detail.html",
        context={
            "user": user,
            "active_page": "issues",
            "active_project": project,
            "project": project,
            "issue": issue,
            "journals": journals,
            "thread_tree": thread_tree,
            "members": members,
            "lookup_maps": lookup_maps,
            "reactions_by_journal": reactions_by_journal,
            **tab_ctx,
            "relations": relations,
            "watchers": watchers,
            "is_watching": is_watching,
            "last_activity_at": last_activity_at,
            "activity_page": activity_page,
            "activity_per_page": activity_per_page,
            "activity_total_pages": activity_total_pages,
            "activity_total": activity_total,
            "activity_per_page_options": ACTIVITY_PER_PAGE_OPTIONS,
            **lookups,
        },
    )


@router.get("/projects/{project_key}/issues/{issue_ref}/edit/", response_class=HTMLResponse)
async def issue_edit_form(
    project_key: str,
    issue_ref: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the issue edit form."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    try:
        issue = await _issue_svc.get_by_display_key_with_relations(db, issue_ref, user=user)
    except NotFoundError:
        return JSONResponse({"detail": "Issue not found"}, status_code=404)

    lookups = await _get_lookups(db)
    members = await _project_svc.list_members(db, project)
    versions_result = await db.execute(select(Version).where(Version.project_id == project.id).order_by(Version.name))
    versions = list(versions_result.scalars().all())

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/form.html",
        context={
            "user": user,
            "active_page": "issues",
            "active_project": project,
            "project": project,
            "issue": issue,
            "mode": "edit",
            "members": members,
            "versions": versions,
            **lookups,
        },
    )


@router.get("/projects/{project_key}/issues/{issue_ref}/versions/{journal_id}/", response_class=HTMLResponse)
async def issue_description_diff(
    project_key: str,
    issue_ref: str,
    journal_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render a description diff page for a specific journal entry."""
    import difflib

    from specivo.models.journal import JournalDetail

    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    try:
        issue = await _issue_svc.get_by_display_key_with_relations(db, issue_ref, user=user)
    except NotFoundError:
        return JSONResponse({"detail": "Issue not found"}, status_code=404)

    journal_result = await db.execute(
        select(JournalDetail).where(
            JournalDetail.journal_id == journal_id,
            JournalDetail.prop_key == "description",
        )
    )
    detail = journal_result.scalar_one_or_none()
    if detail is None:
        return JSONResponse({"detail": "Description change not found"}, status_code=404)

    old_text = detail.old_value or ""
    new_text = detail.new_value or ""

    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile="Previous",
            tofile="Current",
            lineterm="",
        )
    )

    # Load all description versions for the history table
    from specivo.models.journal import Journal
    from specivo.models.user import User as UserModel

    all_desc_changes = (
        await db.execute(
            select(
                JournalDetail.journal_id,
                JournalDetail.old_value,
                JournalDetail.new_value,
                Journal.created_at,
                Journal.user_id,
                UserModel.login,
                UserModel.display_name,
            )
            .join(Journal, JournalDetail.journal_id == Journal.id)
            .join(UserModel, Journal.user_id == UserModel.id)
            .where(JournalDetail.prop_key == "description", Journal.issue_id == issue.id)
            .order_by(Journal.created_at.desc())
        )
    ).all()

    # Build version list: newest first, numbered from len down to 1
    versions = []
    for idx, row in enumerate(all_desc_changes):
        version_num = len(all_desc_changes) - idx
        versions.append(
            {
                "num": version_num,
                "journal_id": row[0],
                "author_login": row[5],
                "author_name": row[6] or row[5],
                "date": row[3],
                "is_current": idx == 0,
                "is_selected": row[0] == journal_id,
            }
        )

    # Find selected version number for display
    selected_version = next((v for v in versions if v["is_selected"]), None)
    selected_num = selected_version["num"] if selected_version else "?"
    prev_num = selected_num - 1 if selected_num != "?" and selected_num > 1 else None

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/description_diff.html",
        context={
            "user": user,
            "active_page": "issues",
            "active_project": project,
            "project": project,
            "issue": issue,
            "old_text": old_text,
            "new_text": new_text,
            "selected_num": selected_num,
            "prev_num": prev_num,
            "versions": versions,
            "diff_lines": diff_lines,
        },
    )


@router.post("/projects/{project_key}/issues/{issue_ref}/versions/{journal_id}/restore/", response_class=HTMLResponse)
async def restore_description_version(
    project_key: str,
    issue_ref: str,
    journal_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Restore a previous description version by PATCHing the issue."""
    from specivo.models.journal import JournalDetail
    from specivo.services.permission_service import check_permission

    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    try:
        issue = await _issue_svc.get_by_display_key_with_relations(db, issue_ref, user=user)
    except NotFoundError:
        return JSONResponse({"detail": "Issue not found"}, status_code=404)

    if not await check_permission(user, issue.project_id, "edit_issues", db):
        return JSONResponse({"detail": "Permission denied"}, status_code=403)

    detail_result = await db.execute(
        select(JournalDetail).where(
            JournalDetail.journal_id == journal_id,
            JournalDetail.prop_key == "description",
        )
    )
    detail = detail_result.scalar_one_or_none()
    if detail is None:
        return JSONResponse({"detail": "Description version not found"}, status_code=404)

    # Restore old_value as the new description
    from specivo.schemas.issue import IssueUpdate

    update_data = IssueUpdate(description=detail.old_value or "", lock_version=issue.lock_version)
    await _issue_svc.update(db, issue, update_data, user)

    return RedirectResponse(
        f"/projects/{project_key}/issues/{issue_ref}/",
        status_code=302,
    )
