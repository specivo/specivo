"""Web auth pages: login, logout, API keys management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.web.deps import get_current_user_optional, get_templates

router = APIRouter(tags=["web-auth"], include_in_schema=False)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """Render the login page (standalone layout, no sidebar)."""
    templates = get_templates()
    return templates.TemplateResponse(request, "pages/auth/login.html")


@router.get("/logout")
async def logout_page() -> RedirectResponse:
    """Clear auth cookies and redirect to login."""
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/api/v1/auth")
    return response


@router.get("/my/api-keys", response_model=None)
async def api_keys_page(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the API keys management page (requires auth)."""
    user = await get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/auth/api_keys.html",
        context={"user": user, "active_page": "api-keys"},
    )
