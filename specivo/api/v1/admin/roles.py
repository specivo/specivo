"""Admin roles API: list, create, edit, reorder and delete roles, and the permission catalogue.

Served at ``/api/v1/admin/roles/``. Every route is admin-only; an admin API
key is accepted (nothing here hands out a way to sign in), a
project-limited key is refused by ``require_admin_api``. The web role
editor writes only through these routes, so they are the one place the
``role_created`` / ``role_updated`` / ``role_deleted`` audit events are
written. An audit failure is logged and never fails the change.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.admin import require_admin_api
from specivo.core.constants import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT
from specivo.core.database import DB_SESSION
from specivo.models.role import Role
from specivo.models.user import User
from specivo.schemas.common import PaginatedResponse
from specivo.schemas.role import (
    PermissionCatalogueOut,
    RoleCreate,
    RoleDetailOut,
    RoleMove,
    RoleOut,
    RoleProjectOut,
    RoleUpdate,
)
from specivo.services.permission_catalogue import FULL_ACCESS, catalogue_payload
from specivo.services.role_service import RoleService
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-roles"])
_service = RoleService()
_audit = SecurityAuditService()


def role_out(role: Role, membership_count: int = 0) -> RoleOut:
    """The API shape of a role."""
    return RoleOut(
        id=role.id,
        name=role.name,
        position=role.position,
        assignable=role.assignable,
        builtin=role.builtin,
        system_key=role.system_key,
        full_access=FULL_ACCESS in role.permissions,
        permissions=[str(p) for p in role.permissions],
        issues_visibility=role.issues_visibility,
        users_visibility=role.users_visibility,
        time_entries_visibility=role.time_entries_visibility,
        ai_usage_visibility=role.ai_usage_visibility,
        lock_version=role.lock_version,
        membership_count=membership_count,
        created_at=role.created_at,
        updated_at=role.updated_at,
    )


async def _audit_role(
    db: AsyncSession,
    request: Request,
    admin: User,
    event: AuditEvent,
    role_id: int,
    role_name: str,
    details: dict[str, Any],
) -> None:
    """Write a role audit event; a failure is logged and never fails the change."""
    try:
        await _audit.log_role_change(
            db,
            event,
            user_id=admin.id,
            role_id=role_id,
            role_name=role_name,
            details=details,
            request=request,
        )
    except Exception:
        logger.warning("Audit write failed for %s on role %s", event, role_id, exc_info=True)


@router.get("/admin/roles/", response_model=PaginatedResponse[RoleOut])
async def list_roles(
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = DB_SESSION,
    offset: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
) -> PaginatedResponse[RoleOut]:
    """List roles: custom roles in their order, then the builtin Non member role."""
    roles = await _service.list_roles(db)
    counts = await _service.membership_counts(db)
    page = roles[offset : offset + limit]
    return PaginatedResponse[RoleOut](
        total_count=len(roles),
        offset=offset,
        limit=limit,
        items=[role_out(r, counts.get(r.id, 0)) for r in page],
    )


@router.get("/admin/roles/permissions/", response_model=PermissionCatalogueOut)
async def permission_catalogue(admin: Annotated[User, Depends(require_admin_api)]) -> PermissionCatalogueOut:
    """Every permission, grouped and explained, with its prerequisites and the Non member ceiling.

    ``requires`` lists every prerequisite (direct or not): saving a role adds
    them automatically. Labels are in the request's language.
    """
    return PermissionCatalogueOut.model_validate(catalogue_payload())


@router.get("/admin/roles/{role_id}/", response_model=RoleDetailOut)
async def get_role(
    role_id: int,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = DB_SESSION,
) -> RoleDetailOut:
    """Return a role with the projects whose memberships hold it and its workflow row counts."""
    role = await _service.get(db, role_id)
    usage = await _service.usage(db, role_id)
    transitions, rules = await _service.workflow_counts(db, role_id)
    return RoleDetailOut(
        **role_out(role, usage.membership_count).model_dump(),
        projects=[RoleProjectOut(**p) for p in usage.projects],
        workflow_transition_count=transitions,
        field_rule_count=rules,
    )


@router.post("/admin/roles/", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(
    data: RoleCreate,
    request: Request,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = DB_SESSION,
) -> RoleOut:
    """Create a role, blank or as a copy of another (``copy_from_role_id``).

    A copy takes the permissions, visibility settings, ``assignable`` and the
    workflow transitions and field rules. A blank role has no workflow
    transitions, so it cannot change issue status until some are added.
    Permission prerequisites are added automatically. A name already taken
    (ignoring case) returns 409 ``role_name_taken``.
    """
    role, details = await _service.create(db, data)
    await _audit_role(db, request, admin, AuditEvent.ROLE_CREATED, role.id, role.name, details)
    await db.commit()  # commit before response to avoid reload race condition
    return role_out(role)


@router.patch("/admin/roles/{role_id}/", response_model=RoleOut)
async def update_role(
    role_id: int,
    data: RoleUpdate,
    request: Request,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = DB_SESSION,
) -> RoleOut:
    """Edit a role's name, ``assignable``, permissions or visibility settings.

    Send the ``lock_version`` you read; a role changed since returns 409
    ``conflict_lock_version``. Only the fields present are applied.
    ``permissions: ["*"]`` is Full access; otherwise prerequisites are added
    automatically. Built-in roles keep their name and are never assignable
    (422), and the Non member role refuses permissions outside its ceiling
    (422 ``role_permission_not_allowed``, ``details.not_allowed``). A request
    that changes nothing writes no audit event.
    """
    role, changes = await _service.update(db, role_id, data)
    if changes:
        await _audit_role(db, request, admin, AuditEvent.ROLE_UPDATED, role.id, role.name, changes)
    await db.commit()  # commit before response to avoid reload race condition
    counts = await _service.membership_counts(db)
    return role_out(role, counts.get(role.id, 0))


@router.post("/admin/roles/{role_id}/move/", response_model=list[RoleOut])
async def move_role(
    role_id: int,
    data: RoleMove,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = DB_SESSION,
) -> list[RoleOut]:
    """Move a role one place up or down in member pickers; return the custom roles in their new order.

    Moving past either end, or moving a builtin role, changes nothing.
    Reordering does not change ``lock_version`` and is not audited.
    """
    roles = await _service.move(db, role_id, data.direction)
    await db.commit()
    counts = await _service.membership_counts(db)
    return [role_out(r, counts.get(r.id, 0)) for r in roles]


@router.delete("/admin/roles/{role_id}/", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """Delete a role and its workflow rows.

    Refused with 409 ``role_protected`` for a built-in role
    (``details.reason = "builtin"``) or the merge automation role
    (``"automation"``), and with 409 ``role_in_use`` while any project
    membership holds it (``details.projects`` lists where).
    """
    facts = await _service.delete(db, role_id)
    await _audit_role(db, request, admin, AuditEvent.ROLE_DELETED, role_id, facts["role_name"], facts)
    await db.commit()  # commit before response to avoid reload race condition
    return Response(status_code=status.HTTP_204_NO_CONTENT)
