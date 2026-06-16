"""Htmx partials for issue pages — return HTML fragments, not full pages."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from specivo.services.project_service import ProjectService
from specivo.services.reaction_service import ReactionService
from specivo.web.deps import get_current_user_optional, get_templates
from specivo.web.thread_tree import build_thread_tree

if TYPE_CHECKING:
    from specivo.models.user import User

router = APIRouter(prefix="/partials", tags=["web-partials"], include_in_schema=False)

_issue_svc = IssueService()
_journal_svc = JournalService()
_project_svc = ProjectService()
_reaction_svc = ReactionService()


@router.get("/issues/table/", response_class=HTMLResponse)
async def issue_table_partial(
    request: Request,
    project_key: str = Query(...),
    db: AsyncSession = Depends(get_db),  # noqa: B008
    status: str = Query("open"),
    tracker_id: str = Query(""),
    assigned_to_id: str = Query(""),
    priority_id: str = Query(""),
    sort: str = Query("created_at:desc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> Response:
    """Return issue table rows as an HTML fragment for htmx swapping."""
    from specivo.core.utils import safe_int

    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
        await _project_svc.require_project_access(db, project, user)
    except NotFoundError:
        return HTMLResponse("<tr><td colspan='7'>Project not found</td></tr>", status_code=404)

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

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/_table_rows.html",
        context={
            "issues": issues,
            "total": total,
            "project": project,
            "user": user,
            "offset": offset,
            "limit": limit,
        },
    )


@router.get("/issues/{issue_ref}/activity/", response_class=HTMLResponse)
async def issue_activity_partial(
    issue_ref: str,
    request: Request,
    activity_page: int | None = Query(None, ge=1),
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Return the activity/journal list as an HTML fragment for htmx swapping."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        issue = await _issue_svc.get_by_display_key(db, issue_ref, user=user)
    except NotFoundError:
        return HTMLResponse("<p>Issue not found</p>", status_code=404)

    from specivo.core.constants import ACTIVITY_DEFAULT_PER_PAGE, ACTIVITY_PER_PAGE_OPTIONS

    all_journals = await _journal_svc.list_for_issue(db, issue.id, include_private=user.is_admin)

    # Paginate activity feed
    activity_per_page = user.preferences.get("activity_per_page", ACTIVITY_DEFAULT_PER_PAGE)
    if activity_per_page not in ACTIVITY_PER_PAGE_OPTIONS:
        activity_per_page = ACTIVITY_DEFAULT_PER_PAGE

    activity_total = len(all_journals)
    activity_total_pages = max(1, (activity_total + activity_per_page - 1) // activity_per_page)
    # See pages/issues.py::issue_detail for the rationale: when the caller
    # does not pin a page, default to the last (newest) one so freshly
    # written journal entries are visible after a save -> reload.
    if activity_page is None:
        activity_page = activity_total_pages
    elif activity_page > activity_total_pages:
        activity_page = activity_total_pages

    activity_start = (activity_page - 1) * activity_per_page
    journals = all_journals[activity_start : activity_start + activity_per_page]
    thread_tree = build_thread_tree(journals)

    # Load emoji reactions for paginated journals
    journal_ids = [j.id for j in journals]
    reactions_by_journal_raw = await _reaction_svc.list_reactions_bulk(db, journal_ids, user.id)
    reactions_by_journal = {
        jid: [{"emoji": r.emoji, "count": r.count, "reacted_by_me": r.reacted_by_me} for r in groups]
        for jid, groups in reactions_by_journal_raw.items()
    }

    # Build lookup maps for human-readable activity details
    statuses = (await db.execute(select(IssueStatus).order_by(IssueStatus.position))).scalars().all()
    trackers = (await db.execute(select(Tracker).order_by(Tracker.position))).scalars().all()
    priorities = (
        (await db.execute(select(IssuePriority).where(IssuePriority.active.is_(True)).order_by(IssuePriority.position)))
        .scalars()
        .all()
    )

    # Resolve project for member list
    project = await _project_svc.get_by_key(db, issue.project_key)
    members = await _project_svc.list_members(db, project)

    lookup_maps: dict[str, dict[str, str]] = {
        "status_id": {str(s.id): s.name for s in statuses},
        "tracker_id": {str(t.id): t.name for t in trackers},
        "priority_id": {str(p.id): p.name for p in priorities},
        "assigned_to_id": {
            str(m.get("user_id", m.get("id", ""))): m.get("login", m.get("display_name", "")) for m in members
        },
    }

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/_activity.html",
        context={
            "issue": issue,
            "journals": journals,
            "thread_tree": thread_tree,
            "user": user,
            "lookup_maps": lookup_maps,
            "reactions_by_journal": reactions_by_journal,
            "activity_page": activity_page,
            "activity_per_page": activity_per_page,
            "activity_total_pages": activity_total_pages,
            "activity_total": activity_total,
            "activity_per_page_options": ACTIVITY_PER_PAGE_OPTIONS,
        },
    )


@router.get("/issues/{issue_ref}/attachments/", response_class=HTMLResponse)
async def issue_attachments_partial(
    issue_ref: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Return the attachments tab content as an HTML fragment for htmx.

    Lazy-loaded on first click of the Attachments tab in the issue detail page
    so the main page render skips the attachments SELECT entirely.
    """
    import json

    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        issue = await _issue_svc.get_by_display_key(db, issue_ref, user=user)
    except NotFoundError:
        return HTMLResponse("<p>Issue not found</p>", status_code=404)

    # Enforce project access (mirrors issue_detail permission model).
    try:
        project = await _project_svc.get_by_key(db, issue.project_key)
    except NotFoundError:
        return HTMLResponse("<p>Project not found</p>", status_code=404)
    await _project_svc.require_project_access(db, project, user)

    attachments = await _issue_svc.list_attachments(db, issue.id)

    issue_attachments_json = json.dumps(
        [
            {
                "id": att.id,
                "filename": att.filename,
                "content_type": att.content_type or "application/octet-stream",
                "filesize": att.filesize,
                "author": {
                    "id": att.author_id,
                    "name": att.author.display_name or att.author.login,
                },
                "created_at": att.created_at.isoformat() if att.created_at else None,
            }
            for att in attachments
        ]
    )

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/_tab_attachments.html",
        context={
            "issue": issue,
            "issue_attachments_json": issue_attachments_json,
            "user": user,
        },
    )
