"""Web dashboard and notifications pages."""

from __future__ import annotations

import datetime
from typing import cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.core.database import get_db
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.lookups import IssueStatus
from specivo.models.project import Project
from specivo.models.sprint import Sprint
from specivo.models.time_entry import TimeEntry
from specivo.models.user import User
from specivo.models.version import Version
from specivo.models.wiki import Wiki, WikiContent, WikiPage
from specivo.services.issue_service import IssueService
from specivo.services.notification_service import NotificationService
from specivo.services.project_service import ProjectService
from specivo.web.deps import get_current_user_optional, get_templates

router = APIRouter(tags=["web-dashboard"], include_in_schema=False)

_project_svc = ProjectService()
_issue_svc = IssueService()
_notif_svc = NotificationService()


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the main dashboard page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    # list_projects already enforces visibility (admins see all,
    # regular users see public + their memberships).
    projects, total_projects = await _project_svc.list_projects(db, user, offset=0, limit=10)
    notifications, _ = await _notif_svc.list_notifications(db, user.id, limit=5)
    unread_count = await _notif_svc.get_unread_count(db, user.id)

    # Derive the set of project IDs the user is allowed to see.
    # All downstream queries are scoped through this set.
    visible_pids = {p.id for p in projects}

    # ------------------------------------------------------------------
    # My open issues (assigned to user, not closed), sorted by updated_at.
    # Scoped to visible projects. First page (10 items) + total count.
    # ------------------------------------------------------------------
    my_issues: list[Issue] = []
    my_issues_total = 0
    my_issues_limit = 10
    if visible_pids:
        _open_where = (
            Issue.assigned_to_id == user.id,
            IssueStatus.category != "closed",
            Issue.project_id.in_(visible_pids),
        )
        count_stmt = (
            select(func.count())
            .select_from(Issue)
            .join(IssueStatus, Issue.status_id == IssueStatus.id)
            .where(*_open_where)
        )
        my_issues_total = (await db.execute(count_stmt)).scalar() or 0

        my_issues_stmt = (
            select(Issue)
            .join(IssueStatus, Issue.status_id == IssueStatus.id)
            .where(*_open_where)
            .options(
                selectinload(Issue.tracker),
                selectinload(Issue.status),
                selectinload(Issue.priority),
            )
            .order_by(Issue.updated_at.desc())
            .limit(my_issues_limit)
        )
        my_issues = list((await db.execute(my_issues_stmt)).scalars().all())

    # Batch-load project names for the issues
    issue_project_ids = list({i.project_id for i in my_issues})
    if issue_project_ids:
        proj_rows = (
            await db.execute(
                select(Project.id, Project.name).where(Project.id.in_(issue_project_ids))
            )
        ).all()
        proj_names = {r.id: r.name for r in proj_rows}
        for issue in my_issues:
            issue.project_name = proj_names.get(issue.project_id, "")  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Active versions (2 most relevant: open first, then recent closed).
    # Scoped to visible projects — no admin-level list_all().
    # ------------------------------------------------------------------
    active_versions: list[dict] = []
    today = datetime.date.today()
    if visible_pids:
        # Fetch versions with issue progress counts, scoped to visible projects
        done_sub = (
            select(IssueStatus.id).where(IssueStatus.category.in_(["done", "closed"])).scalar_subquery()
        )
        ver_stmt = (
            select(
                Version,
                Project.key.label("project_key"),
                func.count(Issue.id).label("total"),
                func.count(Issue.id).filter(Issue.status_id.in_(done_sub)).label("closed_count"),
            )
            .join(Project, Project.id == Version.project_id)
            .outerjoin(Issue, Issue.fixed_version_id == Version.id)
            .where(Version.project_id.in_(visible_pids))
            .group_by(Version.id, Project.key)
            .order_by(
                # open first, then by due date
                (Version.status == "open").desc(),
                Version.effective_date.asc().nullslast(),
            )
            .limit(4)  # fetch a few extra to pick best 2
        )
        ver_rows = (await db.execute(ver_stmt)).all()

        open_versions = []
        closed_versions = []
        for version, project_key, total, closed in ver_rows:
            closed = int(closed or 0)
            total = int(total or 0)
            progress = int(closed / total * 100) if total > 0 else 0
            v_dict: dict = {
                "name": version.name,
                "status": version.status,
                "due_date": version.effective_date.strftime("%b %-d") if version.effective_date else None,
                "progress": progress,
                "open_count": total - closed,
                "closed_count": closed,
                "total_count": total,
                "project_key": project_key,
            }
            if version.status == "open" and version.effective_date:
                v_dict["days_left"] = (version.effective_date - today).days
            if version.status == "open":
                open_versions.append(v_dict)
            else:
                closed_versions.append(v_dict)

        active_versions = (open_versions + closed_versions)[:2]

    # ------------------------------------------------------------------
    # Active sprints across visible projects (one per project max).
    # Show sprint name, status, dates, duration, and issue progress.
    # ------------------------------------------------------------------
    active_sprints: list[dict] = []
    if visible_pids:
        done_sub = (
            select(IssueStatus.id).where(IssueStatus.category.in_(["done", "closed"])).scalar_subquery()
        )
        sprint_stmt = (
            select(
                Sprint,
                Project.key.label("project_key"),
                Project.name.label("project_name"),
                func.count(Issue.id).label("total"),
                func.count(Issue.id).filter(Issue.status_id.in_(done_sub)).label("closed_count"),
            )
            .join(Project, Project.id == Sprint.project_id)
            .outerjoin(Issue, Issue.sprint_id == Sprint.id)
            .where(Sprint.project_id.in_(visible_pids), Sprint.status == "active")
            .group_by(Sprint.id, Project.key, Project.name)
            .order_by(Sprint.start_date.asc().nullslast())
        )
        sprint_rows = (await db.execute(sprint_stmt)).all()

        for sprint, project_key, project_name, total, closed in sprint_rows:
            closed = int(closed or 0)
            total = int(total or 0)
            progress = int(closed / total * 100) if total > 0 else 0
            days_total = None
            days_remaining = None
            if sprint.start_date and sprint.end_date:
                days_total = (sprint.end_date - sprint.start_date).days
                days_remaining = max(0, (sprint.end_date - today).days)
            active_sprints.append(
                {
                    "id": sprint.id,
                    "name": sprint.name,
                    "project_key": project_key,
                    "project_name": project_name,
                    "start_date": sprint.start_date.strftime("%b %-d") if sprint.start_date else None,
                    "end_date": sprint.end_date.strftime("%b %-d") if sprint.end_date else None,
                    "days_total": days_total,
                    "days_remaining": days_remaining,
                    "total": total,
                    "closed": closed,
                    "progress": progress,
                }
            )

    # ------------------------------------------------------------------
    # Project stats (open issue counts per project)
    # ------------------------------------------------------------------
    all_project_ids = list(visible_pids)
    project_stats = (
        await _project_svc.load_project_stats(db, all_project_ids) if all_project_ids else {}
    )

    # ------------------------------------------------------------------
    # Recent wiki pages (3 most recently updated, scoped to visible projects)
    # ------------------------------------------------------------------
    recent_wiki_pages: list[dict] = []
    if visible_pids:
        wiki_stmt = (
            select(
                WikiPage.title,
                WikiPage.slug,
                WikiPage.updated_at,
                Project.key.label("project_key"),
                User.display_name.label("author_name"),
            )
            .join(Wiki, WikiPage.wiki_id == Wiki.id)
            .join(Project, Wiki.project_id == Project.id)
            .outerjoin(
                WikiContent,
                (WikiContent.page_id == WikiPage.id)
                & (
                    WikiContent.version
                    == (
                        select(func.max(WikiContent.version))
                        .where(WikiContent.page_id == WikiPage.id)
                        .correlate(WikiPage)
                        .scalar_subquery()
                    )
                ),
            )
            .outerjoin(User, WikiContent.author_id == User.id)
            .where(WikiPage.deleted_at.is_(None), Wiki.project_id.in_(visible_pids))
            .order_by(WikiPage.updated_at.desc())
            .limit(3)
        )
        wiki_rows = (await db.execute(wiki_stmt)).all()
        recent_wiki_pages = [
            {
                "title": r.title,
                "slug": r.slug,
                "project_key": r.project_key,
                "updated_at": r.updated_at,
                "author_name": r.author_name or "",
            }
            for r in wiki_rows
        ]

    # ------------------------------------------------------------------
    # Stats: resolved this week, feedback count.
    # Both are scoped to the user's own issues in visible projects.
    # ------------------------------------------------------------------
    week_start = today - datetime.timedelta(days=today.weekday())

    resolved_this_week = 0
    feedback_count = 0
    if visible_pids:
        resolved_stmt = (
            select(func.count())
            .select_from(Issue)
            .join(IssueStatus, Issue.status_id == IssueStatus.id)
            .where(
                Issue.assigned_to_id == user.id,
                Issue.project_id.in_(visible_pids),
                IssueStatus.category.in_(["done", "closed"]),
                Issue.updated_at >= week_start,
            )
        )
        resolved_this_week = (await db.execute(resolved_stmt)).scalar() or 0

        feedback_stmt = (
            select(func.count())
            .select_from(Issue)
            .where(
                Issue.assigned_to_id == user.id,
                Issue.project_id.in_(visible_pids),
                Issue.status_id == 4,
            )
        )
        feedback_count = (await db.execute(feedback_stmt)).scalar() or 0

    # ------------------------------------------------------------------
    # Recent service account activity (last 20 events).
    # Journals from service-account users in visible projects, plus
    # time entries logged by service accounts.
    # ------------------------------------------------------------------
    sa_activity: list[dict] = []
    if visible_pids:
        # Find service account user IDs
        sa_user_stmt = select(User.id, User.login).where(User.is_service_account.is_(True))
        sa_rows = (await db.execute(sa_user_stmt)).all()
        sa_user_ids = [r.id for r in sa_rows]
        sa_user_names = {r.id: r.login for r in sa_rows}

        if sa_user_ids:
            # --- Journals: issue status changes, comments, wiki edits ---
            journal_stmt = (
                select(Journal)
                .options(
                    selectinload(Journal.user),
                    selectinload(Journal.details),
                )
                .where(
                    Journal.user_id.in_(sa_user_ids),
                    Journal.project_id.in_(visible_pids),
                )
                .order_by(Journal.created_at.desc())
                .limit(20)
            )
            journals = list((await db.execute(journal_stmt)).scalars().all())

            # Batch-load issue display keys for journal entries
            journal_issue_ids = [j.issue_id for j in journals if j.issue_id]
            issue_key_map: dict[int, str] = {}
            issue_subject_map: dict[int, str] = {}
            if journal_issue_ids:
                ik_rows = (
                    await db.execute(
                        select(Issue.id, Issue.project_key, Issue.sequence_number, Issue.subject).where(
                            Issue.id.in_(journal_issue_ids)
                        )
                    )
                ).all()
                for r in ik_rows:
                    issue_key_map[r.id] = f"{r.project_key}-{r.sequence_number}"
                    issue_subject_map[r.id] = r.subject

            # Batch-load wiki page titles
            journal_wiki_ids = [j.wiki_page_id for j in journals if j.wiki_page_id]
            wiki_title_map: dict[int, tuple[str, str]] = {}  # id -> (title, project_key)
            if journal_wiki_ids:
                wp_rows = (
                    await db.execute(
                        select(WikiPage.id, WikiPage.title, Project.key.label("project_key"))
                        .join(Wiki, WikiPage.wiki_id == Wiki.id)
                        .join(Project, Wiki.project_id == Project.id)
                        .where(WikiPage.id.in_(journal_wiki_ids))
                    )
                ).all()
                wiki_title_map = {r.id: (r.title, r.project_key) for r in wp_rows}

            for j in journals:
                actor = sa_user_names.get(j.user_id, "agent")
                entry: dict = {"ts": j.created_at, "actor": actor}

                # Determine activity type from journal details
                if j.issue_id:
                    issue_key = issue_key_map.get(j.issue_id, "")
                    issue_subject = issue_subject_map.get(j.issue_id, "")

                    # Check for status change to resolved/closed
                    status_detail = next(
                        (d for d in j.details if d.prop_key == "status_id"),
                        None,
                    )
                    if status_detail and status_detail.new_value in ("3", "5"):
                        entry.update(
                            type="resolved",
                            issue_key=issue_key,
                            summary=issue_subject,
                        )
                    elif j.notes and not j.details:
                        entry.update(type="comment", issue_key=issue_key, summary=j.notes[:80])
                    elif j.details:
                        entry.update(type="updated", issue_key=issue_key, summary=issue_subject)
                    else:
                        continue
                elif j.wiki_page_id:
                    wiki_info = wiki_title_map.get(j.wiki_page_id, ("", ""))
                    entry.update(
                        type="wiki_edit",
                        wiki_title=wiki_info[0],
                        project_key=wiki_info[1],
                    )
                else:
                    continue

                sa_activity.append(entry)

            # --- Time entries from service accounts ---
            te_stmt = (
                select(TimeEntry)
                .options(
                    selectinload(TimeEntry.activity),
                )
                .where(
                    TimeEntry.user_id.in_(sa_user_ids),
                    TimeEntry.project_id.in_(visible_pids),
                )
                .order_by(TimeEntry.created_at.desc())
                .limit(10)
            )
            time_entries = list((await db.execute(te_stmt)).scalars().all())

            # Batch-load issue keys for time entries
            te_issue_ids = [te.issue_id for te in time_entries if te.issue_id]
            if te_issue_ids:
                te_ik_rows = (
                    await db.execute(
                        select(Issue.id, Issue.project_key, Issue.sequence_number).where(
                            Issue.id.in_(te_issue_ids)
                        )
                    )
                ).all()
                for r in te_ik_rows:
                    issue_key_map[r.id] = f"{r.project_key}-{r.sequence_number}"

            for te in time_entries:
                actor = sa_user_names.get(te.user_id, "agent")
                issue_key = issue_key_map.get(te.issue_id, "") if te.issue_id else ""
                activity_name = te.activity.name if te.activity else "Development"
                sa_activity.append(
                    {
                        "ts": te.created_at,
                        "actor": actor,
                        "type": "time_logged",
                        "issue_key": issue_key,
                        "hours": float(te.hours),
                        "activity_name": activity_name,
                    }
                )

            # Sort combined activity by timestamp desc, limit 20
            sa_activity.sort(key=lambda x: x["ts"], reverse=True)
            sa_activity = sa_activity[:20]

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/dashboard.html",
        context={
            "user": user,
            "active_page": "dashboard",
            "projects": projects,
            "total_projects": total_projects,
            "notifications": notifications,
            "unread_count": unread_count,
            "my_issues": my_issues,
            "my_issues_total": my_issues_total,
            "my_issues_offset": 0,
            "my_issues_limit": my_issues_limit,
            "active_versions": active_versions,
            "project_stats": project_stats,
            "recent_wiki_pages": recent_wiki_pages,
            "resolved_this_week": resolved_this_week,
            "feedback_count": feedback_count,
            "active_sprints": active_sprints,
            "sa_activity": sa_activity,
        },
    )


@router.get("/my/notifications/", response_class=HTMLResponse)
async def notifications_page(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    unread_only: bool = Query(False),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> Response:
    """Render the notifications page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    notifications, total = await _notif_svc.list_notifications(
        db, user.id, unread_only=unread_only, offset=offset, limit=limit
    )
    unread_count = await _notif_svc.get_unread_count(db, user.id)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/notifications.html",
        context={
            "user": user,
            "active_page": "notifications",
            "notifications": notifications,
            "total": total,
            "unread_count": unread_count,
            "unread_only": unread_only,
            "offset": offset,
            "limit": limit,
        },
    )
