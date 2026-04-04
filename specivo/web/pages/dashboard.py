"""Web dashboard and notifications pages."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.services.issue_service import IssueService
from specivo.services.notification_service import NotificationService
from specivo.services.project_service import ProjectService
from specivo.web.deps import get_current_user_optional, get_templates

if TYPE_CHECKING:
    from specivo.models.user import User

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

    projects, total_projects = await _project_svc.list_projects(db, user, offset=0, limit=10)
    notifications, _ = await _notif_svc.list_notifications(db, user.id, limit=5)
    unread_count = await _notif_svc.get_unread_count(db, user.id)

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
