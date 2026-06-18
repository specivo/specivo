"""Web admin pages: dashboard, workflows, settings, email, agent groups, kill switch, metadata presets."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.database import get_db
from specivo.models.user import User
from specivo.models.user import User as UserModel
from specivo.services.api_key_service import ApiKeyService
from specivo.services.settings_service import SettingsService
from specivo.services.version_service import VersionService
from specivo.services.workflow_service import WorkflowService
from specivo.web.deps import get_current_user_optional, get_templates

router = APIRouter(tags=["web-admin"], include_in_schema=False)

_settings_svc = SettingsService()
_version_svc = VersionService()
_workflow_svc = WorkflowService()
_api_key_svc = ApiKeyService()


# ---------------------------------------------------------------------------
# Shared admin dependency
# ---------------------------------------------------------------------------


async def require_admin(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> User:
    """Dependency: return the admin user or abort with redirect/403.

    Uses HTTPException with headers trick for redirect; raises 403 for non-admins.
    """
    from fastapi import HTTPException

    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        raise HTTPException(status_code=307, headers={"Location": "/login/"})
    if not user_obj.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_obj  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/admin/", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the admin dashboard."""
    stats = await _settings_svc.get_dashboard_stats(db)
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/dashboard.html",
        context={"user": user, "active_page": "admin", "stats": stats},
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get("/admin/users/", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the admin users page."""
    from specivo.models.role import Role

    result = await db.execute(select(UserModel).order_by(UserModel.id))
    users = list(result.scalars().all())

    roles_result = await db.execute(
        select(Role).where(Role.builtin == 0, Role.assignable.is_(True)).order_by(Role.position)
    )
    roles = [{"id": r.id, "name": r.name} for r in roles_result.scalars().all()]

    users_data = [
        {
            "id": u.id,
            "login": u.login,
            "email": u.email,
            "display_name": u.display_name,
            "is_admin": u.is_admin,
            "is_service_account": u.is_service_account,
            "status": u.status,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "avatar_color": u.preferences.get("avatar_color", "#c49a3c"),
        }
        for u in users
    ]

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/users.html",
        context={
            "user": user,
            "active_page": "admin",
            "users_data": users_data,
            "roles": roles,
        },
    )


@router.get("/admin/users/{user_id}/", response_class=HTMLResponse)
async def admin_user_detail(
    request: Request,
    user_id: int,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the admin user detail page with API key management."""
    from fastapi import HTTPException

    result = await db.execute(select(UserModel).where(UserModel.id == user_id))
    target_user = result.scalar_one_or_none()
    if target_user is None:
        raise HTTPException(status_code=404, detail="User not found")

    api_keys = await _api_key_svc.list_keys(session=db, user_id=target_user.id)
    keys_data = [
        {
            "id": k.id,
            "name": k.name,
            "key_prefix": k.key_prefix,
            "is_active": k.is_active,
            "created_at": k.created_at.isoformat() if k.created_at else None,
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        }
        for k in api_keys
    ]

    target_data = {
        "id": target_user.id,
        "login": target_user.login,
        "email": target_user.email,
        "display_name": target_user.display_name,
        "is_admin": target_user.is_admin,
        "is_service_account": target_user.is_service_account,
        "status": target_user.status,
        "created_at": target_user.created_at.isoformat() if target_user.created_at else None,
        "last_login_at": target_user.last_login_at.isoformat() if target_user.last_login_at else None,
        "avatar_color": target_user.preferences.get("avatar_color", "#c49a3c"),
    }

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/user_detail.html",
        context={
            "user": user,
            "active_page": "admin",
            "target_user": target_data,
            "api_keys": keys_data,
        },
    )


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


@router.get("/admin/projects/", response_class=HTMLResponse)
async def admin_projects(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the admin projects management page."""
    from specivo.core.constants import DEFAULT_PROJECT_COLORS
    from specivo.services.project_service import ProjectService

    svc = ProjectService()
    # Load all projects including archived (status 9)
    all_projects = await svc.list_all_admin(db, user)
    project_ids = [p.id for p in all_projects]
    stats = await svc.load_project_stats(db, project_ids) if project_ids else {}

    # Serialize for Alpine.js
    projects_data = []
    for p in all_projects:
        s = stats.get(p.id, {})
        issue_count = s.get("open_count", 0) + s.get("closed_count", 0)
        projects_data.append(
            {
                "id": p.id,
                "key": p.key,
                "identifier": p.identifier,
                "name": p.name,
                "description": p.description or "",
                "color": p.color or "#c49a3c",
                "status": p.status,
                "issue_count": issue_count,
                "member_count": s.get("member_count", 0),
                "has_issues": issue_count > 0,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
        )

    # For parent dropdown (active projects only)
    active_projects = [{"key": p.key, "name": p.name} for p in all_projects if p.status != 9]

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/projects.html",
        context={
            "user": user,
            "active_page": "admin",
            "projects_data": projects_data,
            "all_projects": active_projects,
            "project_colors": DEFAULT_PROJECT_COLORS,
        },
    )


# ---------------------------------------------------------------------------
# Versions (cross-project)
# ---------------------------------------------------------------------------


@router.get("/admin/versions/", response_class=HTMLResponse)
async def admin_versions(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the admin versions page -- all versions across all projects."""
    versions_data = await _version_svc.list_all(db)

    # Extract unique projects for the filter dropdown
    seen_keys: set[str] = set()
    projects_data: list[dict[str, str]] = []
    for v in versions_data:
        if v["project_key"] not in seen_keys:
            seen_keys.add(v["project_key"])
            projects_data.append({"key": v["project_key"], "name": v["project_name"]})

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/versions.html",
        context={
            "user": user,
            "active_page": "admin",
            "versions_data": versions_data,
            "projects_data": projects_data,
        },
    )


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


@router.get("/admin/workflows/", response_class=HTMLResponse)
async def admin_workflows(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the workflow management page."""
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


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get("/admin/settings/", response_class=HTMLResponse)
async def admin_settings(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the settings management page."""
    from zoneinfo import available_timezones

    from specivo.core.locales import LANGUAGE_LABELS, get_available_locales

    settings = await _settings_svc.get_all(db)
    available = get_available_locales()
    language_choices = [(code, LANGUAGE_LABELS.get(code, code)) for code in available]
    current_default = settings.get("default_language") or get_settings().default_language
    if current_default not in available:
        current_default = "en"

    timezone_choices = sorted(available_timezones())
    current_default_timezone = settings.get("default_timezone") or "UTC"

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/settings.html",
        context={
            "user": user,
            "active_page": "admin",
            "settings": settings,
            "language_choices": language_choices,
            "current_default_language": current_default,
            "timezone_choices": timezone_choices,
            "current_default_timezone": current_default_timezone,
        },
    )


@router.post("/admin/settings/language/", response_model=None)
async def admin_settings_language(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
    default_language: str = Form(""),
) -> Response:
    """Persist the workspace default language and update the runtime override.

    Validates the submitted code against the installed locales; an unknown
    code is rejected (the setting is left unchanged) and the admin is
    redirected back without applying it.
    """
    from specivo.core.locales import get_available_locales
    from specivo.core.runtime_settings import set_default_language_override

    code = default_language.strip()
    if code in get_available_locales():
        await _settings_svc.set_many(db, {"default_language": code})
        await db.commit()
        set_default_language_override(code)

    return RedirectResponse("/admin/settings/", status_code=303)


@router.post("/admin/settings/timezone/", response_model=None)
async def admin_settings_timezone(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
    default_timezone: str = Form(""),
) -> Response:
    """Persist the instance-wide default timezone.

    Validates the submitted value against the IANA timezone database; an unknown
    zone is rejected (the setting is left unchanged) and the admin is redirected
    back without applying it.
    """
    from zoneinfo import available_timezones

    tz = default_timezone.strip()
    if tz in available_timezones():
        await _settings_svc.set_many(db, {"default_timezone": tz})
        await db.commit()

    return RedirectResponse("/admin/settings/", status_code=303)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def mask_smtp_host(host: str) -> str:
    """Mask SMTP hostname, keeping first char and domain parts visible."""
    parts = host.split(".")
    if len(parts) <= 1:
        return host[0] + "*" * (len(host) - 1) if host else host
    first = parts[0]
    masked_first = first[0] + "*" * (len(first) - 1) if first else first
    return ".".join([masked_first, *parts[1:]])


def mask_smtp_user(user: str) -> str:
    """Mask SMTP username, showing first 4 and last 3 chars."""
    if len(user) <= 7:
        return user[:1] + "*" * (len(user) - 1)
    return user[:4] + "*" * (len(user) - 7) + user[-3:]


@router.get("/admin/email/", response_class=HTMLResponse)
async def admin_email(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
) -> Response:
    """Render the admin email configuration and test page."""
    settings = get_settings()

    smtp_configured = settings.smtp_host != "localhost" and bool(settings.smtp_user)

    smtp_info = {
        "host": mask_smtp_host(settings.smtp_host) if smtp_configured else settings.smtp_host,
        "port": settings.smtp_port,
        "user": mask_smtp_user(settings.smtp_user) if smtp_configured else "",
        "from": settings.smtp_from,
        "tls": settings.smtp_tls,
        "configured": smtp_configured,
    }

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/email.html",
        context={
            "user": user,
            "active_page": "admin",
            "smtp": smtp_info,
        },
    )


# ---------------------------------------------------------------------------
# Agent Groups (enterprise)
# ---------------------------------------------------------------------------


@router.get("/admin/agent-groups/", response_class=HTMLResponse)
async def admin_agent_groups(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the agent groups management page."""
    from specivo.services.group_policy_service import GroupPolicyService

    groups = await GroupPolicyService().list_groups(db)
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/agent_groups.html",
        context={"user": user, "active_page": "admin", "groups": groups},
    )


# ---------------------------------------------------------------------------
# Kill Switch (enterprise)
# ---------------------------------------------------------------------------


@router.get("/admin/kill-switch/", response_class=HTMLResponse)
async def admin_kill_switch(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the kill switch page."""
    from specivo.services.kill_switch_service import KillSwitchService

    events = await KillSwitchService().list_kill_events(db)
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/kill_switch.html",
        context={"user": user, "active_page": "admin", "events": events},
    )


# ---------------------------------------------------------------------------
# Metadata Presets
# ---------------------------------------------------------------------------


@router.get("/admin/metadata-presets/", response_class=HTMLResponse)
async def admin_metadata_presets(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the admin metadata presets page."""
    from specivo.schemas.metadata_schema import MetadataPresetOut
    from specivo.services.metadata_preset_service import MetadataPresetService

    svc = MetadataPresetService()
    presets = await svc.list_presets(db)
    presets_data = [
        MetadataPresetOut.model_validate(p).model_dump(mode="json")
        for p in presets
    ]

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/metadata_presets.html",
        context={
            "user": user,
            "active_page": "admin",
            "presets_data": presets_data,
        },
    )
