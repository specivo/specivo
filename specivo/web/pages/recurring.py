"""Web recurring-pattern pages: management list and pattern detail.

These mirror the sprint/version web pages: project resolution via
``ProjectService``, optional-cookie auth via ``get_current_user_optional``, and
permission gating through ``check_permission``. Viewing requires ``view_issues``;
managing (create / edit / skip / delete) requires ``manage_recurring_tasks``.

Server rendering provides the no-JS / SEO-friendly initial view (the pattern
list, the schedule summary, and already-generated instances). Alpine.js + the
REST API drive all mutation (create / edit / enable-toggle / delete / skip) and
the live "next occurrences" preview — exactly as the settings versions tab does.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.core.config import get_settings
from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError
from specivo.core.utils import utcnow
from specivo.models.issue import Issue
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import ProjectService
from specivo.services.recurrence import expand_occurrences
from specivo.services.recurring_pattern_service import RecurringPatternService
from specivo.web.deps import get_current_user_optional, get_templates

if TYPE_CHECKING:
    from specivo.models.project import Project
    from specivo.models.recurring_pattern import RecurringPattern
    from specivo.models.user import User

router = APIRouter(tags=["web-recurring"], include_in_schema=False)

_project_svc = ProjectService()
_pattern_svc = RecurringPatternService()

# Number of upcoming occurrences shown in the server-rendered schedule summary.
_PREVIEW_COUNT = 5


async def _resolve_project(
    project_key: str,
    user: User,
    db: AsyncSession,
) -> Project:
    """Resolve a project by key and assert the user may access it."""
    try:
        project = await _project_svc.get_by_key(db, project_key.upper())
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)
    return project


async def _can_manage(user: User, project: Project, db: AsyncSession) -> bool:
    """True if the user may manage recurring tasks in this project."""
    return user.is_admin or await check_permission(user, project.id, Permission.MANAGE_RECURRING_TASKS, db)


async def _can_view(user: User, project: Project, db: AsyncSession) -> bool:
    """True if the user may view issues (and thus recurring patterns)."""
    return user.is_admin or await check_permission(user, project.id, Permission.VIEW_ISSUES, db)


async def _lookups_context(db: AsyncSession) -> dict[str, list[dict]]:
    """Load trackers / statuses / priorities for the create-edit form selects."""
    trackers = (await db.execute(select(Tracker).order_by(Tracker.position))).scalars().all()
    statuses = (await db.execute(select(IssueStatus).order_by(IssueStatus.position))).scalars().all()
    priorities = (await db.execute(select(IssuePriority).order_by(IssuePriority.position))).scalars().all()
    return {
        "trackers_data": [{"id": t.id, "name": t.name} for t in trackers],
        "statuses_data": [{"id": s.id, "name": s.name} for s in statuses],
        "priorities_data": [{"id": p.id, "name": p.name} for p in priorities],
    }


def _pattern_summary(pattern: RecurringPattern) -> dict[str, Any]:
    """Build a JSON-serialisable summary of a pattern for Alpine initial state."""
    return {
        "id": pattern.id,
        "name": pattern.name,
        "enabled": pattern.enabled,
        "freq": pattern.freq,
        "rrule_interval": pattern.rrule_interval,
        "byday": pattern.byday,
        "bymonthday": pattern.bymonthday,
        "bysetpos": pattern.bysetpos,
        "anchor_mode": pattern.anchor_mode,
        "base_date_strategy": pattern.base_date_strategy,
        "timezone": pattern.timezone,
        "creation_lead_time_days": pattern.creation_lead_time_days,
        "template_tracker_id": pattern.template_tracker_id,
        "template_status_id": pattern.template_status_id,
        "template_priority_id": pattern.template_priority_id,
        "template_assigned_to_id": pattern.template_assigned_to_id,
        "template_subject": pattern.template_subject,
        "template_description": pattern.template_description or "",
        "carry_over": pattern.carry_over or {},
        "reset_checklist": pattern.reset_checklist,
        "assignee_rotation": pattern.assignee_rotation,
        "dtstart": pattern.dtstart.isoformat() if pattern.dtstart else None,
    }


@router.get(
    "/projects/{project_key}/recurring-patterns/",
    response_class=HTMLResponse,
)
async def recurring_list(
    project_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the recurring-patterns management list for a project."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    project = await _resolve_project(project_key, user, db)

    if not await _can_view(user, project, db):
        raise HTTPException(status_code=403, detail="Permission denied")

    patterns = await _pattern_svc.list_for_project(db, project.id)
    can_manage = await _can_manage(user, project, db)
    members = await _project_svc.list_members(db, project)
    lookups = await _lookups_context(db)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/projects/recurring_list.html",
        context={
            "user": user,
            "active_page": "recurring",
            "active_project": project,
            "project": project,
            "patterns": patterns,
            "patterns_json": [_pattern_summary(p) for p in patterns],
            "can_manage_recurring": can_manage,
            "members": members,
            **lookups,
        },
    )


@router.get(
    "/projects/{project_key}/recurring-patterns/{pattern_id}/",
    response_class=HTMLResponse,
)
async def recurring_detail(
    project_key: str,
    pattern_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render a single recurring pattern: schedule, upcoming occurrences, instances."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    project = await _resolve_project(project_key, user, db)

    if not await _can_view(user, project, db):
        raise HTTPException(status_code=403, detail="Permission denied")

    try:
        pattern = await _pattern_svc.get_by_id(db, pattern_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Recurring pattern not found")
    if pattern.project_id != project.id:
        raise HTTPException(status_code=404, detail="Recurring pattern not found")

    can_manage = await _can_manage(user, project, db)

    # Upcoming occurrences preview (server-rendered for no-JS / SEO). Bound by
    # the pattern's lead time, capped at the server's configured maximum, so a
    # single page render can never expand an unbounded series.
    settings = get_settings()
    window_days = min(
        pattern.creation_lead_time_days,
        settings.recurring_tasks_max_lead_time_days,
    )
    now = utcnow()
    window_end = now + timedelta(days=window_days)
    exdates, _overrides = await _pattern_svc._load_exceptions(db, pattern.id)
    all_occurrences = expand_occurrences(_pattern_svc.build_spec(pattern), now, window_end, exdates)
    upcoming = all_occurrences[:_PREVIEW_COUNT]

    # Already-generated instances for this pattern (newest occurrence first).
    instance_stmt = (
        select(Issue)
        .where(Issue.recurring_pattern_id == pattern.id)
        .options(
            selectinload(Issue.status),
            selectinload(Issue.tracker),
            selectinload(Issue.assigned_to),
        )
        .order_by(Issue.original_occurrence_at.desc().nullslast())
    )
    instances = list((await db.execute(instance_stmt)).scalars().all())

    members = await _project_svc.list_members(db, project)
    lookups = await _lookups_context(db)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/projects/recurring_pattern_detail.html",
        context={
            "user": user,
            "active_page": "recurring",
            "active_project": project,
            "project": project,
            "pattern": pattern,
            "pattern_json": _pattern_summary(pattern),
            "upcoming_occurrences": upcoming,
            "upcoming_total": len(all_occurrences),
            "instances": instances,
            "can_manage_recurring": can_manage,
            "members": members,
            **lookups,
        },
    )
