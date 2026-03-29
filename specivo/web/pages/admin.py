"""Web admin pages: dashboard, workflows, settings, agent groups, kill switch."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.services.group_policy_service import GroupPolicyService
from specivo.services.kill_switch_service import KillSwitchService
from specivo.services.settings_service import SettingsService
from specivo.services.workflow_service import WorkflowService
from specivo.web.deps import get_current_user_optional, get_templates

if TYPE_CHECKING:
    from specivo.models.user import User

router = APIRouter(tags=["web-admin"], include_in_schema=False)

_workflow_svc = WorkflowService()
_settings_svc = SettingsService()
_group_svc = GroupPolicyService()
_kill_svc = KillSwitchService()


async def _require_admin(request: Request, db: AsyncSession) -> User | None:
    """Check auth and admin role. Returns user or None (caller should return 403/302)."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return None
    user = cast("User", user_obj)
    if not user.is_admin:
        return None
    return user


@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the admin dashboard."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    if not user.is_admin:
        return JSONResponse({"detail": "Admin access required"}, status_code=403)

    # Gather stats
    from specivo.models.agent_session import AgentSession
    from specivo.models.kill_switch import KillEvent
    from specivo.models.project import Project
    from specivo.models.user import User as UserModel

    total_users = (await db.execute(select(func.count()).select_from(UserModel))).scalar_one()
    active_projects = (
        await db.execute(select(func.count()).select_from(Project).where(Project.status == 1))
    ).scalar_one()
    agent_sessions = (await db.execute(select(func.count()).select_from(AgentSession))).scalar_one()
    kill_events = (await db.execute(select(func.count()).select_from(KillEvent))).scalar_one()

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/dashboard.html",
        context={
            "user": user,
            "active_page": "admin",
            "stats": {
                "total_users": total_users,
                "active_projects": active_projects,
                "agent_sessions": agent_sessions,
                "kill_events": kill_events,
            },
        },
    )


@router.get("/admin/workflows", response_class=HTMLResponse)
async def admin_workflows(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the workflow management page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    if not user.is_admin:
        return JSONResponse({"detail": "Admin access required"}, status_code=403)

    from specivo.models.lookups import IssueStatus, Tracker
    from specivo.models.role import Role

    trackers = (await db.execute(select(Tracker).order_by(Tracker.position))).scalars().all()
    statuses = (await db.execute(select(IssueStatus).order_by(IssueStatus.position))).scalars().all()
    roles = (await db.execute(select(Role).order_by(Role.position))).scalars().all()

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/workflows.html",
        context={
            "user": user,
            "active_page": "admin",
            "trackers": list(trackers),
            "statuses": list(statuses),
            "roles": list(roles),
        },
    )


@router.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the settings management page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    if not user.is_admin:
        return JSONResponse({"detail": "Admin access required"}, status_code=403)

    settings = await _settings_svc.get_all(db)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/settings.html",
        context={
            "user": user,
            "active_page": "admin",
            "settings": settings,
        },
    )


@router.get("/admin/agent-groups", response_class=HTMLResponse)
async def admin_agent_groups(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the agent groups management page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    if not user.is_admin:
        return JSONResponse({"detail": "Admin access required"}, status_code=403)

    groups = await _group_svc.list_groups(db)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/agent_groups.html",
        context={
            "user": user,
            "active_page": "admin",
            "groups": groups,
        },
    )


@router.get("/admin/kill-switch", response_class=HTMLResponse)
async def admin_kill_switch(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the kill switch page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    if not user.is_admin:
        return JSONResponse({"detail": "Admin access required"}, status_code=403)

    events = await _kill_svc.list_kill_events(db)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/kill_switch.html",
        context={
            "user": user,
            "active_page": "admin",
            "events": events,
        },
    )
