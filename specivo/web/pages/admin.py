"""Web admin pages: dashboard, workflows, roles, trackers, settings, email, agent groups, kill switch, presets."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.database import DB_SESSION
from specivo.models.user import User
from specivo.models.user import User as UserModel
from specivo.services.api_key_scope import is_global_admin
from specivo.services.api_key_service import ApiKeyService
from specivo.services.security_audit_service import (
    EVENT_CATEGORIES,
    AuditCategory,
    AuditEvent,
    SecurityAuditService,
    events_in,
    is_audit_event,
)
from specivo.services.settings_service import SettingsService
from specivo.services.version_service import VersionService
from specivo.services.workflow_service import WorkflowService
from specivo.web.deps import get_current_user_optional, get_templates

router = APIRouter(tags=["web-admin"], include_in_schema=False)

_settings_svc = SettingsService()
_version_svc = VersionService()
_workflow_svc = WorkflowService()
_api_key_svc = ApiKeyService()
_audit_svc = SecurityAuditService()


# ---------------------------------------------------------------------------
# Shared admin dependency
# ---------------------------------------------------------------------------


async def require_admin(
    request: Request,
    db: AsyncSession = DB_SESSION,
) -> User:
    """Dependency: return the admin user or abort with redirect/403.

    Uses HTTPException with headers trick for redirect; raises 403 for non-admins.
    """
    from fastapi import HTTPException

    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        raise HTTPException(status_code=307, headers={"Location": "/login/"})
    if not isinstance(user_obj, User) or not is_global_admin(user_obj):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_obj  # type: ignore[return-value]


async def require_admin_session(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
) -> User:
    """An admin signed in with a session; an admin API key sent as a Bearer header is refused.

    The web layer also accepts ``Authorization: Bearer spv_...``, and a request
    without a session cookie is not CSRF-checked, so a form that hands out a
    way to sign in (reactivating an account, the sign-in settings) would
    otherwise be a side door around the session-only admin API routes
    (``specivo.api.v1.admin.require_admin_session_api``).
    """
    from fastapi import HTTPException

    if getattr(request.state, "api_key_id", None) is not None:
        raise HTTPException(status_code=403, detail="A signed-in session is required")
    return user


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/admin/", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
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
    db: AsyncSession = DB_SESSION,
) -> Response:
    """Render the admin users page."""
    from specivo.models.role import Role
    from specivo.services.anonymous_user_service import real_users_clause

    result = await db.execute(select(UserModel).where(real_users_clause()).order_by(UserModel.id))
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
    db: AsyncSession = DB_SESSION,
    account: str = "",
) -> Response:
    """Render the admin user detail page: account, status, connected SSO accounts, API keys.

    ``?account=`` only ever selects one of the fixed, translated messages of
    the deactivate / reactivate forms.
    """
    from fastapi import HTTPException

    result = await db.execute(select(UserModel).where(UserModel.id == user_id))
    target_user = result.scalar_one_or_none()
    # The anonymous user is not listed, so it has no page either.
    if target_user is None or target_user.is_anonymous:
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
        "must_change_password": target_user.must_change_password,
        "status": target_user.status,
        "created_at": target_user.created_at.isoformat() if target_user.created_at else None,
        "last_login_at": target_user.last_login_at.isoformat() if target_user.last_login_at else None,
        "avatar_color": target_user.preferences.get("avatar_color", "#c49a3c"),
    }

    from specivo.services.sso.identity_service import IdentityService

    identities = [] if target_user.is_service_account else await IdentityService().list_for_user(db, target_user.id)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/user_detail.html",
        context={
            "user": user,
            "active_page": "admin",
            "target_user": target_data,
            "api_keys": keys_data,
            "identities": identities,
            "is_self": target_user.id == user.id,
            "account_message": _account_messages().get(account),
            "account_error": _account_errors().get(account),
        },
    )


def _account_messages() -> dict[str, str]:
    from specivo.core.i18n import gettext as _

    return {
        "deactivated": _(
            "Account deactivated. The user was signed out everywhere and their API keys were switched off."
        ),
        "reactivated": _(
            "Account reactivated. The user can sign in again. API keys stay inactive: "
            "activate them again or create new ones."
        ),
        "email_changed": _("Email address changed. A notice went to the old and the new address."),
    }


def _account_errors() -> dict[str, str]:
    from specivo.core.i18n import gettext as _

    return {
        "self": _("You cannot deactivate your own account."),
        "last_admin": _("This is the last active administrator. Make another user an administrator first."),
        "email_invalid": _("Enter a valid email address."),
        "email_same": _("This is already the user's email address."),
        "email_taken": _("A user with this email already exists"),
    }


async def _target_or_404(db: AsyncSession, user_id: int) -> User:
    from fastapi import HTTPException

    target = await db.scalar(select(UserModel).where(UserModel.id == user_id))
    if target is None or target.is_anonymous:
        raise HTTPException(status_code=404, detail="User not found")
    return target


def _user_page_redirect(user_id: int, account: str) -> RedirectResponse:
    prefix = get_settings().stealth_prefix.rstrip("/")
    return RedirectResponse(f"{prefix}/admin/users/{user_id}/?account={account}", status_code=303)


@router.post("/admin/users/{user_id}/deactivate/", response_model=None)
async def admin_deactivate_user(
    user_id: int,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> RedirectResponse:
    """Deactivate an account from its admin page: every session and API key ends."""
    from specivo.services.user_service import LastActiveAdminError, UserService

    target = await _target_or_404(db, user_id)
    if target.id == user.id:
        return _user_page_redirect(user_id, "self")
    try:
        await UserService().deactivate(db, target, reason="admin", actor_id=user.id)
    except LastActiveAdminError:
        return _user_page_redirect(user_id, "last_admin")
    await db.commit()
    return _user_page_redirect(user_id, "deactivated")


@router.post("/admin/users/{user_id}/reactivate/", response_model=None)
async def admin_reactivate_user(
    user_id: int,
    user: Annotated[User, Depends(require_admin_session)],
    db: AsyncSession = DB_SESSION,
) -> RedirectResponse:
    """Reactivate an account from its admin page; its API keys stay inactive."""
    from specivo.services.user_service import UserService

    target = await _target_or_404(db, user_id)
    await UserService().reactivate(db, target, actor_id=user.id)
    await db.commit()
    return _user_page_redirect(user_id, "reactivated")


@router.post("/admin/users/{user_id}/email/", response_model=None)
async def admin_change_user_email(
    user_id: int,
    user: Annotated[User, Depends(require_admin_session)],
    db: AsyncSession = DB_SESSION,
    email: Annotated[str, Form(max_length=320)] = "",
) -> RedirectResponse:
    """Change an account's address directly (set and marked verified); audited, both addresses notified.

    Session-only like the API route: the address receives password reset links.
    """
    from specivo.core.exceptions import AppError, ConflictError
    from specivo.services.email_change_service import EmailChangeService

    target = await _target_or_404(db, user_id)
    try:
        await EmailChangeService().admin_set_email(db, target, email, admin=user)
    except ConflictError:
        return _user_page_redirect(user_id, "email_taken")
    except AppError as exc:
        same = exc.code == "email_change_same_address"
        return _user_page_redirect(user_id, "email_same" if same else "email_invalid")
    await db.commit()
    return _user_page_redirect(user_id, "email_changed")


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


@router.get("/admin/projects/", response_class=HTMLResponse)
async def admin_projects(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """Render the admin projects management page."""
    from specivo.core.constants import DEFAULT_PROJECT_COLORS
    from specivo.services.project_service import ProjectService

    svc = ProjectService()
    # Load all projects including archived (status 9)
    all_projects = await svc.list_all_admin(db, user)
    project_ids = [p.id for p in all_projects]
    stats = await svc.load_project_stats(db, project_ids, reader=user) if project_ids else {}

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
                # People with access, direct or through a group — not the
                # number of membership rows. The table has a separate Groups
                # column so the two are never conflated.
                "people_count": s.get("member_count", 0),
                "group_count": s.get("group_count", 0),
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
# User groups
# ---------------------------------------------------------------------------

# Groups are an administrative concept and a self-hosted instance has tens of
# them, not thousands, so the list page renders them all and filters in the
# browser rather than paging the API. The cap exists only so a pathological
# instance degrades into a visible notice instead of an enormous page; the
# template says so when it is hit.
GROUP_LIST_CAP = 500


@router.get("/admin/groups/", response_class=HTMLResponse)
async def admin_groups(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """Render the admin user groups page."""
    from specivo.services.user_group_service import UserGroupService

    svc = UserGroupService()
    rows, total = await svc.list_groups(db, limit=GROUP_LIST_CAP)

    groups_data = [
        {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"] or "",
            "user_count": row["user_count"],
            "project_count": row["project_count"],
        }
        for row in rows
    ]

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/groups.html",
        context={
            "user": user,
            "active_page": "admin",
            "groups_data": groups_data,
            "total_groups": total,
            "list_cap": GROUP_LIST_CAP,
        },
    )


@router.get("/admin/groups/{group_id}/", response_class=HTMLResponse)
async def admin_group_detail(
    request: Request,
    group_id: int,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """Render one group: the users in it, and the projects it grants access to."""
    from fastapi import HTTPException

    from specivo.core.exceptions import NotFoundError
    from specivo.services.user_group_service import UserGroupService

    svc = UserGroupService()
    try:
        group = await svc.get(db, group_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Group not found")

    users, user_total = await svc.list_users(db, group_id, limit=GROUP_LIST_CAP)
    projects = await svc.list_projects(db, group_id)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/group_detail.html",
        context={
            "user": user,
            "active_page": "admin",
            "group": {
                "id": group.id,
                "name": group.name,
                "description": group.description or "",
            },
            "group_users": users,
            "user_total": user_total,
            "group_projects": projects,
        },
    )


# ---------------------------------------------------------------------------
# Versions (cross-project)
# ---------------------------------------------------------------------------


@router.get("/admin/versions/", response_class=HTMLResponse)
async def admin_versions(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
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
    db: AsyncSession = DB_SESSION,
) -> Response:
    """Render the workflow management page."""
    from specivo.models.lookups import IssueStatus, Tracker
    from specivo.models.role import Role

    trackers = (await db.execute(select(Tracker).order_by(Tracker.position))).scalars().all()
    statuses = (await db.execute(select(IssueStatus).order_by(IssueStatus.position))).scalars().all()
    # Builtin roles are never held through a membership, so no transition could apply to them.
    roles = (await db.execute(select(Role).where(Role.builtin == 0).order_by(Role.position))).scalars().all()

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
# Roles and trackers: visibility settings
# ---------------------------------------------------------------------------


@router.get("/admin/roles/", response_class=HTMLResponse)
async def admin_roles(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """Render the roles page: each role's user and time entry visibility."""
    from specivo.models.role import Role

    roles = (await db.execute(select(Role).order_by(Role.builtin, Role.position, Role.id))).scalars().all()
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/roles.html",
        context={"user": user, "active_page": "admin", "roles": list(roles)},
    )


@router.post("/admin/roles/{role_id}/", response_model=None)
async def admin_role_update(
    role_id: int,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    users_visibility: str = Form(""),
    time_entries_visibility: str = Form(""),
) -> Response:
    """Save a role's ``users_visibility`` and ``time_entries_visibility``.

    Unknown values are ignored (left unchanged), like the settings form.
    Builtin roles are editable here too: only their ``builtin`` is protected.
    """
    from fastapi import HTTPException

    from specivo.models.role import TIME_ENTRIES_VISIBILITY_VALUES, USERS_VISIBILITY_VALUES, Role

    role = await db.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")
    if users_visibility in USERS_VISIBILITY_VALUES:
        role.users_visibility = users_visibility
    if time_entries_visibility in TIME_ENTRIES_VISIBILITY_VALUES:
        role.time_entries_visibility = time_entries_visibility
    await db.commit()
    return RedirectResponse("/admin/roles/", status_code=303)


@router.get("/admin/trackers/", response_class=HTMLResponse)
async def admin_trackers(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """Render the trackers page: each tracker's private-by-default flag."""
    from specivo.models.lookups import Tracker

    trackers = (await db.execute(select(Tracker).order_by(Tracker.position, Tracker.id))).scalars().all()
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/trackers.html",
        context={"user": user, "active_page": "admin", "trackers": list(trackers)},
    )


@router.post("/admin/trackers/{tracker_id}/", response_model=None)
async def admin_tracker_update(
    tracker_id: int,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    private_by_default: str = Form(""),
) -> Response:
    """Save a tracker's ``private_by_default``; an unticked checkbox sends nothing, meaning off."""
    from fastapi import HTTPException

    from specivo.core.lookup_cache import invalidate_lookups
    from specivo.models.lookups import Tracker

    tracker = await db.get(Tracker, tracker_id)
    if tracker is None:
        raise HTTPException(status_code=404, detail="Tracker not found")
    tracker.private_by_default = private_by_default == "1"
    await db.commit()
    # The issue form reads trackers from the process-local lookup cache.
    invalidate_lookups()
    return RedirectResponse("/admin/trackers/", status_code=303)


# ---------------------------------------------------------------------------
# Currencies and the reporting currency
# ---------------------------------------------------------------------------

_CURRENCY_FILTERS = ("all", "active", "inactive")
_CURRENCY_ERRORS = frozenset({"currency_unknown", "currency_inactive"})


@router.get("/admin/currencies/", response_class=HTMLResponse)
async def admin_currencies(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    show: str = Query("all"),
    q: str = Query(""),
    error: str = Query(""),
) -> Response:
    """Render the currencies page: the instance reporting currency and each currency's switch."""
    from specivo.core.i18n import get_locale
    from specivo.core.money import currency_display_name, currency_display_symbol
    from specivo.services.currency_service import CurrencyService

    service = CurrencyService()
    locale = get_locale()
    show = show if show in _CURRENCY_FILTERS else "all"
    needle = q.strip().lower()

    all_currencies = await service.list_currencies(db)
    reporting_currency = await service.get_instance_reporting_currency(db)
    rows = []
    for c in all_currencies:
        name = currency_display_name(c.code, locale, fallback=c.name)
        if show == "active" and not c.is_active:
            continue
        if show == "inactive" and c.is_active:
            continue
        if needle and needle not in c.code.lower() and needle not in name.lower() and needle not in c.name.lower():
            continue
        rows.append(
            {
                "code": c.code,
                "name": name,
                "iso_name": c.name,
                "symbol": currency_display_symbol(c.code, locale),
                "minor_units": c.minor_units,
                "kind": c.kind,
                "is_active": c.is_active,
                "is_reporting": c.code == reporting_currency,
            }
        )
    reporting_choices = [
        {"code": c.code, "name": currency_display_name(c.code, locale, fallback=c.name)}
        for c in all_currencies
        if c.is_active or c.code == reporting_currency
    ]
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/currencies.html",
        context={
            "user": user,
            "active_page": "admin",
            "currencies": rows,
            "reporting_currency": reporting_currency,
            "reporting_choices": reporting_choices,
            "show": show,
            "q": q.strip(),
            "active_count": sum(1 for c in all_currencies if c.is_active),
            "total_count": len(all_currencies),
            "error": error if error in _CURRENCY_ERRORS else "",
        },
    )


def _currencies_redirect(show: str, q: str, error: str = "") -> RedirectResponse:
    params = {k: v for k, v in (("show", show), ("q", q.strip()), ("error", error)) if v and v != "all"}
    url = "/admin/currencies/" + (f"?{urlencode(params)}" if params else "")
    return RedirectResponse(url, status_code=303)


@router.post("/admin/currencies/reporting/", response_model=None)
async def admin_currencies_reporting(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    code: str = Form(""),
) -> Response:
    """Set the instance reporting currency; an unknown or disabled code is refused with a notice."""
    from specivo.core.exceptions import AppError
    from specivo.services.currency_service import CurrencyService

    try:
        await CurrencyService().set_instance_reporting_currency(db, code, user, request=request)
    except AppError as exc:
        return _currencies_redirect("all", "", exc.code)
    await db.commit()
    return _currencies_redirect("all", "")


@router.post("/admin/currencies/{code}/", response_model=None)
async def admin_currency_toggle(
    code: str,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    is_active: str = Form(""),
    show: str = Form("all"),
    q: str = Form(""),
) -> Response:
    """Enable (``is_active=1``) or disable a currency in pickers, then return to the same view."""
    from fastapi import HTTPException

    from specivo.core.exceptions import NotFoundError
    from specivo.services.currency_service import CurrencyService

    try:
        await CurrencyService().set_active(db, code, is_active == "1", user, request=request)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Currency not found")
    await db.commit()
    return _currencies_redirect(show if show in _CURRENCY_FILTERS else "all", q)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get("/admin/settings/", response_class=HTMLResponse)
async def admin_settings(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
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

    # Search indexing (FTS): seed the card minimally; the Alpine component
    # fetches fresh state (running job, last result, reindex_needed) on init.
    from specivo.core.config import _ALLOWED_FTS_LANGUAGES
    from specivo.tasks.search import reindex_needed_key

    fts_language = settings.get("search_fts_language") or get_settings().search_fts_language
    fts_reindex_needed = settings.get(reindex_needed_key(None)) == "1"

    # Anonymous access has its own card with a confirmation step, so it is
    # kept out of the generic key/value table (whose API refuses the key).
    from specivo.services.anonymous_access_service import (
        ANONYMOUS_ACCESS_SETTING_KEY,
        is_anonymous_access_enabled,
        list_projects_with_anonymous_permissions,
    )

    anonymous_access_enabled = await is_anonymous_access_enabled(db)
    anonymous_access_projects = await list_projects_with_anonymous_permissions(db)
    # The similar-issues switch is a checkbox in the Defaults card.
    from specivo.services.similar_issues import SIMILAR_ISSUES_SETTING_KEY, instance_setting_enabled

    similar_issues_enabled = instance_setting_enabled(settings.get(SIMILAR_ISSUES_SETTING_KEY))
    # The sso.* authentication settings have their own card (and API) too, and
    # the reporting currency is set on the currencies page.
    from specivo.services.currency_service import REPORTING_CURRENCY_SETTING_KEY
    from specivo.services.exchange_rate_service import EXCHANGE_RATE_SETTING_KEYS
    from specivo.services.sso.auth_settings import AUTH_SETTING_KEYS, get_auth_settings
    from specivo.services.sso.provider_service import ProviderService

    hidden_keys = (
        AUTH_SETTING_KEYS
        | EXCHANGE_RATE_SETTING_KEYS
        | {
            ANONYMOUS_ACCESS_SETTING_KEY,
            SIMILAR_ISSUES_SETTING_KEY,
            REPORTING_CURRENCY_SETTING_KEY,
        }
    )
    generic_settings = {k: v for k, v in settings.items() if k not in hidden_keys}
    auth_settings = await get_auth_settings(db)
    sso_providers, _ = await ProviderService().list_providers(db)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/settings.html",
        context={
            "user": user,
            "active_page": "admin",
            "settings": generic_settings,
            "anonymous_access_enabled": anonymous_access_enabled,
            "anonymous_access_projects": anonymous_access_projects,
            "language_choices": language_choices,
            "current_default_language": current_default,
            "timezone_choices": timezone_choices,
            "current_default_timezone": current_default_timezone,
            "fts_language": fts_language,
            "fts_allowed": sorted(_ALLOWED_FTS_LANGUAGES),
            "fts_reindex_needed": fts_reindex_needed,
            "similar_issues_enabled": similar_issues_enabled,
            "auth_settings": auth_settings,
            "sso_providers": [
                {"slug": p.slug, "display_name": p.display_name, "enabled": p.enabled} for p in sso_providers
            ],
            "sso_force_local_login": get_settings().sso_force_local_login,
        },
    )


@router.post("/admin/settings/defaults/", response_model=None)
async def admin_settings_defaults(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    default_language: str = Form(""),
    default_timezone: str = Form(""),
    fts_language: str = Form(""),
    similar_issues_enabled: str = Form(""),
) -> Response:
    """Persist the workspace defaults (UI language, timezone, FTS analyzer
    language, similar-issue suggestions) in a single save.

    Each field is validated independently — language against the installed
    locales, timezone against the IANA database, FTS language against the
    allowed regconfig list. Unknown values are ignored (left unchanged) while
    valid values are saved. The runtime language override is refreshed whenever
    the UI language is updated, and changing the FTS analyzer language flags a
    reindex as needed.
    """
    from zoneinfo import available_timezones

    from specivo.core.config import _ALLOWED_FTS_LANGUAGES
    from specivo.core.locales import get_available_locales
    from specivo.core.runtime_settings import set_default_language_override
    from specivo.tasks.search import reindex_needed_key

    to_set: dict[str, str | None] = {}
    code = default_language.strip()
    if code and code in get_available_locales():
        to_set["default_language"] = code
    tz = default_timezone.strip()
    if tz and tz in available_timezones():
        to_set["default_timezone"] = tz
    fts = fts_language.strip()
    if fts and fts in _ALLOWED_FTS_LANGUAGES:
        current_fts = (await _settings_svc.get(db, "search_fts_language")) or get_settings().search_fts_language
        to_set["search_fts_language"] = fts
        if fts != current_fts:
            to_set[reindex_needed_key(None)] = "1"

    # The checkbox follows a hidden "false" input of the same name, so a ticked
    # box arrives as "true" (the last value wins) and an unticked one as "false".
    # Any other value, or none, leaves the switch unchanged.
    if similar_issues_enabled in ("true", "false"):
        from specivo.services.similar_issues import SIMILAR_ISSUES_SETTING_KEY

        to_set[SIMILAR_ISSUES_SETTING_KEY] = similar_issues_enabled

    if to_set:
        await _settings_svc.set_many(db, to_set)
        await db.commit()
        if "default_language" in to_set:
            set_default_language_override(to_set["default_language"])

    return RedirectResponse("/admin/settings/", status_code=303)


def _render_anonymous_access_confirm(
    request: Request,
    user: User,
    projects: list,
    *,
    stale: bool = False,
    status_code: int = 200,
) -> Response:
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/anonymous_access_confirm.html",
        context={"user": user, "active_page": "admin", "projects": projects, "stale": stale},
        status_code=status_code,
    )


@router.post("/admin/settings/anonymous-access/", response_model=None)
async def admin_settings_anonymous_access(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    enabled: str = Form(""),
    confirm: str = Form(""),
    confirmed_projects: list[str] = Form(default=[]),  # noqa: B008
) -> Response:
    """Turn the anonymous access switch on or off.

    ``enabled=1`` without ``confirm=1`` changes nothing and redirects to the
    confirmation page. That page posts ``confirm=1`` with the keys it showed as
    ``confirmed_projects``; if the opted-in projects changed in the meantime,
    it is rendered again with the current list and a notice (409). Turning the
    switch off needs no confirmation.
    """
    from specivo.services.anonymous_access_service import (
        AnonymousAccessConfirmationRequiredError,
        AnonymousAccessConfirmationStaleError,
        set_anonymous_access_enabled,
    )

    try:
        await set_anonymous_access_enabled(
            db,
            enabled == "1",
            user,
            confirmed_projects=confirmed_projects if confirm == "1" else None,
            request=request,
        )
    except AnonymousAccessConfirmationRequiredError:
        return RedirectResponse("/admin/settings/anonymous-access/confirm/", status_code=303)
    except AnonymousAccessConfirmationStaleError as exc:
        return _render_anonymous_access_confirm(request, user, exc.projects, stale=True, status_code=409)
    await db.commit()
    return RedirectResponse("/admin/settings/", status_code=303)


@router.get("/admin/settings/anonymous-access/confirm/", response_class=HTMLResponse)
async def admin_settings_anonymous_access_confirm(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """Ask the administrator to confirm turning anonymous access on, naming the opted-in projects."""
    from specivo.services.anonymous_access_service import (
        is_anonymous_access_enabled,
        list_projects_with_anonymous_permissions,
    )

    if await is_anonymous_access_enabled(db):
        return RedirectResponse("/admin/settings/", status_code=303)
    return _render_anonymous_access_confirm(request, user, await list_projects_with_anonymous_permissions(db))


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
    db: AsyncSession = DB_SESSION,
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
    db: AsyncSession = DB_SESSION,
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
    db: AsyncSession = DB_SESSION,
) -> Response:
    """Render the admin metadata presets page."""
    from specivo.schemas.metadata_schema import MetadataPresetOut
    from specivo.services.metadata_preset_service import MetadataPresetService

    svc = MetadataPresetService()
    presets = await svc.list_presets(db)
    presets_data = [MetadataPresetOut.model_validate(p).model_dump(mode="json") for p in presets]

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


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

#: Without a "from" date the viewer shows this many days, which also lets
#: PostgreSQL skip older partitions.
AUDIT_LOG_DEFAULT_DAYS = 30


def audit_category_labels() -> dict[AuditCategory, str]:
    """The translated label of each audit category, in the order the filter lists them."""
    from specivo.core.i18n import gettext as _

    return {
        AuditCategory.SIGN_IN: _("Sign-in"),
        AuditCategory.ACCESS: _("Access"),
        AuditCategory.AGENTS: _("Agents"),
        AuditCategory.ISSUES_AND_WIKI: _("Issues and wiki"),
        AuditCategory.ADMINISTRATION: _("Administration"),
        AuditCategory.SSO: _("Single sign-on"),
        AuditCategory.PROVISIONING: _("Provisioning"),
        AuditCategory.SESSIONS: _("Sessions"),
    }


def _viewer_zone(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(user.timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@router.get("/admin/audit-log/", response_class=HTMLResponse)
async def admin_audit_log(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    category: str = "",
    event_type: str = "",
    login: str = "",
    project: str = "",
    date_from: str | None = None,
    date_to: str = "",
    ip: str = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Response:
    """Render the security audit log with its filters (a plain GET form).

    Dates are days in the viewer's timezone; ``date_to`` is inclusive.
    Without ``date_from`` in the query the last 30 days are shown; an empty
    ``date_from`` (the field cleared) means no lower bound.
    """
    from specivo.core.i18n import gettext as _
    from specivo.models.project import Project

    zone = _viewer_zone(user)
    labels = audit_category_labels()
    if date_from is None:
        date_from = (datetime.now(zone).date() - timedelta(days=AUDIT_LOG_DEFAULT_DAYS)).isoformat()
    filters = {
        "category": category.strip(),
        "event_type": event_type.strip(),
        "login": login.strip(),
        "project": project.strip(),
        "date_from": date_from.strip(),
        "date_to": date_to.strip(),
        "ip": ip.strip(),
    }

    errors: list[str] = []
    event_types: list[str] | None = None
    if filters["category"]:
        if filters["category"] in AuditCategory.__members__.values():
            event_types = events_in(AuditCategory(filters["category"]))
        else:
            errors.append(_("Unknown category."))
    if filters["event_type"] and not is_audit_event(filters["event_type"]):
        errors.append(_("Unknown event type."))
    user_id: int | None = None
    if filters["login"]:
        user_id = await _audit_svc.user_id_for_login(db, filters["login"])
        if user_id is None:
            errors.append(_("No user has this login."))
    project_id: int | None = None
    if filters["project"]:
        project_id = await db.scalar(select(Project.id).where(func.upper(Project.key) == filters["project"].upper()))
        if project_id is None:
            errors.append(_("No project has this key."))
    after = before = None
    if filters["date_from"]:
        day = _parse_day(filters["date_from"])
        if day is None:
            errors.append(_("The from date is not a valid date."))
        else:
            after = datetime.combine(day, time.min, tzinfo=zone)
    if filters["date_to"]:
        day = _parse_day(filters["date_to"])
        if day is None:
            errors.append(_("The to date is not a valid date."))
        else:
            before = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    if len(filters["ip"]) > 45:
        errors.append(_("The IP address is too long."))

    rows: list[dict[str, Any]] = []
    total = 0
    if not errors:
        items, total = await _audit_svc.list_events(
            db,
            event_type=filters["event_type"] or None,
            event_types=event_types,
            user_id=user_id,
            project_id=project_id,
            ip_address=filters["ip"] or None,
            after=after,
            before=before,
            offset=offset,
            limit=limit,
        )
        logins, keys = await _audit_svc.names_for(db, items)
        for item in items:
            event = AuditEvent(item.event_type) if is_audit_event(item.event_type) else None
            rows.append(
                {
                    "created_at": item.created_at,
                    "event_type": item.event_type,
                    "category": labels[EVENT_CATEGORIES[event]] if event is not None else "",
                    "user_id": item.user_id,
                    "user_login": logins.get(item.user_id) if item.user_id is not None else None,
                    "project_id": item.project_id,
                    "project_key": keys.get(item.project_id) if item.project_id is not None else None,
                    "resource_type": item.resource_type,
                    "resource_id": item.resource_id,
                    "permission": item.permission,
                    "ip_address": item.ip_address,
                    "user_agent": item.user_agent,
                    "request_id": item.request_id,
                    "from_agent": (item.details or {}).get("source") == "mcp",
                    # Rendered escaped in a <pre>, never as Markdown or HTML.
                    "details_json": json.dumps(item.details or {}, indent=2, ensure_ascii=False, sort_keys=True),
                }
            )

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/admin/audit_log.html",
        context={
            "user": user,
            "active_page": "admin",
            "filters": filters,
            "errors": errors,
            "categories": list(labels.items()),
            "event_types": sorted(e.value for e in AuditEvent),
            "rows": rows,
            "total": total,
            "offset": offset,
            "limit": limit,
            "zone": zone.key,
            "page_query": urlencode({k: v for k, v in filters.items() if v or k == "date_from"}),
        },
    )
