"""Htmx partials for the dashboard — return HTML fragments, not full pages.

Each partial renders one block of the dashboard with the same data as the
page (``specivo.web.pages.dashboard``), so a switch or a pager swaps just that
block. Without JavaScript the switches are plain links to the page.
"""

from __future__ import annotations

import datetime
from typing import cast

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import DB_SESSION
from specivo.core.utils import utcnow
from specivo.models.user import User
from specivo.services import dashboard_service as dash
from specivo.web.deps import get_current_user_optional, get_templates
from specivo.web.pages.dashboard import _label_projects, activity_context, spend_context

router = APIRouter(prefix="/partials/dashboard", tags=["web-partials"], include_in_schema=False)


@router.get("/my-issues/", response_class=HTMLResponse)
async def my_issues_partial(
    request: Request,
    db: AsyncSession = DB_SESSION,
    offset: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=50),
) -> Response:
    """Return My Open Issues table as an HTML fragment for htmx swapping."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    today = datetime.date.today()
    scope = await dash.load_scope(db, user)
    my_issues, total = await dash.my_open_issues(db, scope, offset=offset, limit=limit)
    _label_projects(scope, my_issues)
    return get_templates().TemplateResponse(
        request,
        "pages/_my_issues.html",
        context={
            "user": user,
            "my_issues": my_issues,
            "my_issues_total": total,
            "my_issues_offset": offset,
            "my_issues_limit": limit,
            "issue_flags": await dash.issue_flags(db, scope, my_issues, today),
        },
    )


@router.get("/spend/", response_class=HTMLResponse)
async def spend_partial(
    request: Request,
    db: AsyncSession = DB_SESSION,
    period: str = Query("month"),
) -> Response:
    """The AI spend and agent time block for *period* (``today``, ``7d``, ``month``)."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)
    scope = await dash.load_scope(db, user)
    context = {"user": user, **await spend_context(db, scope, period, utcnow().date())}
    if context["spend"] is None:
        # Neither an administrator nor a manager of AI spend in any project.
        return Response(status_code=403)
    return get_templates().TemplateResponse(request, "pages/dashboard/_spend.html", context=context)


@router.get("/activity/", response_class=HTMLResponse)
async def activity_partial(
    request: Request,
    db: AsyncSession = DB_SESSION,
    who: str = Query("all"),
) -> Response:
    """The grouped activity block for *who* (``all``, ``agents``, ``people``)."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)
    scope = await dash.load_scope(db, user)
    context = {"user": user, **await activity_context(db, scope, who)}
    return get_templates().TemplateResponse(request, "pages/dashboard/_activity.html", context=context)
