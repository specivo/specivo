"""Web admin pages for roles: the list, the "who can do what" matrix, the new-role form and the role editor.

Every handler here is a GET. The pages are Alpine components (``adminRoles``,
``adminRoleNew``, ``adminRoleEditor``) that write through the admin JSON API
(``/api/v1/admin/roles/...``), so validation, the dependency rules and the
audit log live in one place: ``RoleService`` behind the REST routes. The
handlers read through ``RoleService`` and the permission catalogue only.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import DB_SESSION
from specivo.core.exceptions import NotFoundError
from specivo.core.i18n import gettext as _
from specivo.models.role import ROLE_SYSTEM_KEY_AUTOMATION, Role, RoleBuiltin
from specivo.models.user import User
from specivo.services.permission_catalogue import ENFORCED_KEYS, FULL_ACCESS, catalogue_payload
from specivo.services.role_service import RoleService
from specivo.web.deps import get_templates
from specivo.web.pages.admin import require_admin

router = APIRouter(tags=["web-admin"], include_in_schema=False)
_service = RoleService()


def _module_hints() -> dict[str, str]:
    """The "only applies where the module is on" note for each catalogue group module."""
    return {
        "issue_tracking": _("Only applies in projects with the Issue tracking module enabled."),
        "wiki": _("Only applies in projects with the Wiki module enabled."),
        "time_tracking": _("Only applies in projects with the Time tracking module enabled."),
    }


def _is_full_access(role: Role) -> bool:
    return FULL_ACCESS in (role.permissions or [])


def _summary(role: Role) -> str:
    """Return "Full access", or how many of the enforced permissions the role holds.

    "Not used yet" permissions count in neither number: holding one grants nothing.
    """
    if _is_full_access(role):
        return _("Full access")
    granted = len(ENFORCED_KEYS.intersection(role.permissions or []))
    return _("%(granted)s of %(total)s") % {"granted": granted, "total": len(ENFORCED_KEYS)}


def _role_row(role: Role, counts: dict[int, int]) -> dict[str, Any]:
    return {
        "id": role.id,
        "name": role.name,
        "builtin": role.builtin != RoleBuiltin.CUSTOM,
        "assignable": bool(role.assignable),
        "full_access": _is_full_access(role),
        "summary": _summary(role),
        "membership_count": counts.get(role.id, 0),
    }


@router.get("/admin/roles/", response_class=HTMLResponse)
async def admin_roles(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """List the roles: memberships, a permission summary, and their order in member pickers."""
    roles = await _service.list_roles(db)
    counts = await _service.membership_counts(db)
    rows = [_role_row(r, counts) for r in roles]
    return get_templates().TemplateResponse(
        request,
        "pages/admin/roles.html",
        context={
            "user": user,
            "active_page": "admin",
            "roles_tab": "list",
            "custom_roles": [r for r in rows if not r["builtin"]],
            "builtin_roles": [r for r in rows if r["builtin"]],
        },
    )


@router.get("/admin/roles/matrix/", response_class=HTMLResponse)
async def admin_roles_matrix(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """Read-only matrix of roles by permissions: "can Developers edit the wiki?" at a glance."""
    roles = await _service.list_roles(db)
    catalogue = catalogue_payload()
    columns = [
        {
            "id": r.id,
            "name": r.name,
            "builtin": r.builtin != RoleBuiltin.CUSTOM,
            "full_access": _is_full_access(r),
            "granted": set(r.permissions or []),
        }
        for r in roles
    ]
    return get_templates().TemplateResponse(
        request,
        "pages/admin/roles_matrix.html",
        context={
            "user": user,
            "active_page": "admin",
            "roles_tab": "matrix",
            "groups": catalogue["groups"],
            "columns": columns,
        },
    )


@router.get("/admin/roles/new/", response_class=HTMLResponse)
async def admin_role_new(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """The new-role form: a name, and a blank role or a copy of an existing one."""
    roles = await _service.list_roles(db)
    return get_templates().TemplateResponse(
        request,
        "pages/admin/role_new.html",
        context={
            "user": user,
            "active_page": "admin",
            "copy_sources": [{"id": r.id, "name": r.name} for r in roles],
        },
    )


def _delete_block(role: Role, membership_count: int) -> str | None:
    """Why the role cannot be deleted, or None. The API enforces the same rules."""
    if role.builtin != RoleBuiltin.CUSTOM:
        return "builtin"
    if role.system_key == ROLE_SYSTEM_KEY_AUTOMATION:
        return "automation"
    if membership_count:
        return "in_use"
    return None


@router.get("/admin/roles/{role_id}/", response_class=HTMLResponse)
async def admin_role_detail(
    request: Request,
    role_id: int,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """The role editor: name, permissions, visibility, and delete."""
    try:
        role = await _service.get(db, role_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Role not found")  # noqa: B904

    usage = await _service.usage(db, role_id)
    transitions, field_rules = await _service.workflow_counts(db, role_id)
    is_custom = role.builtin == RoleBuiltin.CUSTOM
    projects = [{"id": p["id"], "key": p["key"], "name": p["name"]} for p in usage.projects]

    return get_templates().TemplateResponse(
        request,
        "pages/admin/role_detail.html",
        context={
            "user": user,
            "active_page": "admin",
            "role": {
                "id": role.id,
                "name": role.name,
                "builtin": not is_custom,
                "non_member": role.builtin == RoleBuiltin.NON_MEMBER,
                "automation": role.system_key == ROLE_SYSTEM_KEY_AUTOMATION,
                "assignable": bool(role.assignable),
                "permissions": list(role.permissions or []),
                "users_visibility": role.users_visibility,
                "time_entries_visibility": role.time_entries_visibility,
                "ai_usage_visibility": role.ai_usage_visibility,
                "lock_version": role.lock_version,
            },
            "catalogue": catalogue_payload(),
            "module_hints": _module_hints(),
            "membership_count": usage.membership_count,
            "usage_projects": projects,
            "workflow_transition_count": transitions,
            "field_rule_count": field_rules,
            "show_workflow_warning": is_custom and transitions == 0,
            "delete_block": _delete_block(role, usage.membership_count),
        },
    )
