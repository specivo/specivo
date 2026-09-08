"""Admin user groups API — CRUD and membership for the ``UserGroup`` principal.

Served at ``/api/v1/admin/groups/``: "groups" is what the concept is called
to a user.  The module is named ``user_groups`` to keep it apart from
``admin/groups.py``, which is the enterprise **agent** group API
(``/admin/agent-groups/``) and is a different thing entirely.

Every route is admin-only.  Group membership is an access grant — putting a
user into a group hands them every role the group holds on every project —
so all mutating routes write a ``group_change`` audit event.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.admin import require_admin_api
from specivo.core.constants import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT
from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError
from specivo.models.user import User
from specivo.schemas.common import PaginatedResponse
from specivo.schemas.user_group import (
    UserGroupCreate,
    UserGroupDetailOut,
    UserGroupListItem,
    UserGroupOut,
    UserGroupProjectOut,
    UserGroupUpdate,
    UserGroupUserAdd,
    UserGroupUserOut,
)
from specivo.services.security_audit_service import GroupAction, SecurityAuditService
from specivo.services.user_group_service import UNSET, UserGroupService

router = APIRouter(tags=["admin-groups"])
_service = UserGroupService()
_audit = SecurityAuditService()

# Headers carrying the delete report. A 204 has no body, so the counts of what
# the cascade removed travel here; see the delete endpoint's docstring.
REMOVED_USERS_HEADER = "X-Removed-Users"
REMOVED_MEMBERSHIPS_HEADER = "X-Removed-Project-Memberships"


async def _login_of(db: AsyncSession, user_id: int) -> str | None:
    """Return a user's login for the audit trail, or None if they are gone."""
    return (await db.execute(select(User.login).where(User.id == user_id))).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


@router.get("/admin/groups/", response_model=PaginatedResponse[UserGroupListItem])
async def list_groups(
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),
    q: str = Query("", max_length=100, description="Case-insensitive substring match on the group name."),
    offset: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
) -> PaginatedResponse[UserGroupListItem]:
    """List user groups, each with its user count and project count."""
    rows, total = await _service.list_groups(db, q=q or None, offset=offset, limit=limit)
    return PaginatedResponse[UserGroupListItem](
        total_count=total,
        offset=offset,
        limit=limit,
        items=[UserGroupListItem(**row) for row in rows],
    )


@router.post("/admin/groups/", response_model=UserGroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(
    data: UserGroupCreate,
    request: Request,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),
) -> UserGroupOut:
    """Create a user group. A name already taken (ignoring case) returns 409."""
    group = await _service.create(db, name=data.name, description=data.description)

    await _audit.log_group_change(
        session=db,
        action=GroupAction.CREATED,
        user_id=admin.id,
        group_id=group.id,
        group_name=group.name,
        request=request,
    )

    await db.commit()  # commit before response to avoid reload race condition
    return UserGroupOut.model_validate(group)


@router.get("/admin/groups/{group_id}/", response_model=UserGroupDetailOut)
async def get_group(
    group_id: int,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),
) -> UserGroupDetailOut:
    """Return a group with the projects it holds membership on and the roles it grants there.

    The project list is the reason this endpoint exists: deleting a group
    revokes that access everywhere at once, and an admin should be able to see
    it before they do.
    """
    group = await _service.get(db, group_id)
    projects = await _service.list_projects(db, group_id)
    user_count = await _service.count_users(db, group_id)

    return UserGroupDetailOut(
        id=group.id,
        name=group.name,
        description=group.description,
        created_at=group.created_at,
        updated_at=group.updated_at,
        user_count=user_count,
        projects=[UserGroupProjectOut(**p) for p in projects],
    )


@router.patch("/admin/groups/{group_id}/", response_model=UserGroupOut)
async def update_group(
    group_id: int,
    data: UserGroupUpdate,
    request: Request,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),
) -> UserGroupOut:
    """Rename a group and/or edit its description.

    Only the fields present in the body are applied; ``description: null``
    clears it. Renaming onto a name already taken (ignoring case) returns 409.
    """
    fields = data.model_fields_set
    existing = await _service.get(db, group_id)
    old_name = existing.name

    group = await _service.update(
        db,
        group_id,
        name=data.name if "name" in fields else None,
        description=data.description if "description" in fields else UNSET,
    )

    if group.name != old_name:
        await _audit.log_group_change(
            session=db,
            action=GroupAction.RENAMED,
            user_id=admin.id,
            group_id=group.id,
            group_name=group.name,
            extra={"old_name": old_name},
            request=request,
        )

    await db.commit()  # commit before response to avoid reload race condition
    return UserGroupOut.model_validate(group)


@router.delete(
    "/admin/groups/{group_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
    # Declared so the two headers appear in the generated schema. A 204 has no
    # body to describe them in, and an undocumented header is one nobody reads.
    responses={
        status.HTTP_204_NO_CONTENT: {
            "description": "Group deleted, with a count of the access that went with it.",
            "headers": {
                REMOVED_USERS_HEADER: {
                    "description": "Users that were in the group when it was deleted.",
                    "schema": {"type": "integer"},
                },
                REMOVED_MEMBERSHIPS_HEADER: {
                    "description": "Project memberships the group held, now revoked.",
                    "schema": {"type": "integer"},
                },
            },
        }
    },
)
async def delete_group(
    group_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a group, revoking every access it granted.

    The group's user list and its project memberships cascade away with it.
    A 204 carries no body, so what the delete cost is reported in two response
    headers — ``X-Removed-Users`` and ``X-Removed-Project-Memberships`` — and
    recorded in the audit event, where it survives the cascade.
    """
    removed = await _service.delete(db, group_id)

    await _audit.log_group_change(
        session=db,
        action=GroupAction.DELETED,
        user_id=admin.id,
        group_id=removed.group_id,
        group_name=removed.name,
        extra={
            "users_removed": removed.users_removed,
            "project_memberships_removed": removed.project_memberships_removed,
        },
        request=request,
    )

    await db.commit()  # commit before response to avoid reload race condition
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={
            REMOVED_USERS_HEADER: str(removed.users_removed),
            REMOVED_MEMBERSHIPS_HEADER: str(removed.project_memberships_removed),
        },
    )


# ---------------------------------------------------------------------------
# Users inside a group
# ---------------------------------------------------------------------------


@router.get("/admin/groups/{group_id}/users/", response_model=PaginatedResponse[UserGroupUserOut])
async def list_group_users(
    group_id: int,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),
    offset: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
) -> PaginatedResponse[UserGroupUserOut]:
    """List the users in a group."""
    rows, total = await _service.list_users(db, group_id, offset=offset, limit=limit)
    return PaginatedResponse[UserGroupUserOut](
        total_count=total,
        offset=offset,
        limit=limit,
        items=[UserGroupUserOut(**row) for row in rows],
    )


@router.post(
    "/admin/groups/{group_id}/users/",
    response_model=UserGroupUserOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_group_user(
    group_id: int,
    data: UserGroupUserAdd,
    request: Request,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),
) -> UserGroupUserOut:
    """Put a user into a group.

    Adding a user who is already in the group is a no-op and still returns
    201 with the member's row; no audit event is written for the no-op,
    because no access changed.
    """
    added = await _service.add_user(db, group_id, data.user_id)

    user = (await db.execute(select(User).where(User.id == data.user_id))).scalar_one_or_none()
    if user is None:  # pragma: no cover — add_user has already validated this
        raise NotFoundError(f"User {data.user_id} not found")

    if added:
        group = await _service.get(db, group_id)
        await _audit.log_group_change(
            session=db,
            action=GroupAction.USER_ADDED,
            user_id=admin.id,
            group_id=group.id,
            group_name=group.name,
            target_user_id=user.id,
            target_login=user.login,
            request=request,
        )

    await db.commit()  # commit before response to avoid reload race condition
    return UserGroupUserOut(
        user_id=user.id,
        login=user.login,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
    )


@router.delete("/admin/groups/{group_id}/users/{user_id}/", status_code=status.HTTP_204_NO_CONTENT)
async def remove_group_user(
    group_id: int,
    user_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Take a user out of a group. A user who is not in it returns 404."""
    group = await _service.get(db, group_id)
    target_login = await _login_of(db, user_id)

    await _service.remove_user(db, group_id, user_id)

    await _audit.log_group_change(
        session=db,
        action=GroupAction.USER_REMOVED,
        user_id=admin.id,
        group_id=group.id,
        group_name=group.name,
        target_user_id=user_id,
        target_login=target_login,
        request=request,
    )

    await db.commit()  # commit before response to avoid reload race condition
    return Response(status_code=status.HTTP_204_NO_CONTENT)
