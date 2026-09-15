"""Anonymous read access: the per-project opt-in.

``projects.anonymous_permissions`` lists what a visitor without an account may
read in a project. The database limits it to ``view_issues`` / ``view_wiki``
(``ck_projects_anonymous_permissions_allowed``) and keeps it empty unless the
project is public (``ck_projects_anonymous_permissions_public``).

This module is the only writer of that column, and adds the rules the database
cannot express:

- only instance administrators change the list; ``manage_project`` is not
  enough;
- every change is written to the security audit log with the old and the new
  value;
- making a project private clears the list in the same transaction.

Nothing on the request path reads these values. Granting anonymous visitors
access based on them is a separate piece of work.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from fastapi import Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, PermissionDeniedError
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.permission_service import Permission
from specivo.services.security_audit_service import SecurityAuditService

# The only permissions an anonymous visitor can ever hold. Mirrors
# ck_projects_anonymous_permissions_allowed.
ANONYMOUS_PERMISSION_CEILING: frozenset[Permission] = frozenset({Permission.VIEW_ISSUES, Permission.VIEW_WIKI})

_audit = SecurityAuditService()


class AnonymousPermissionsChangeReason(StrEnum):
    """Why a project's anonymous permissions changed (audit ``details.reason``)."""

    ADMIN_UPDATE = "admin_update"
    PROJECT_MADE_PRIVATE = "project_made_private"


def normalize_anonymous_permissions(permissions: Iterable[str]) -> list[str]:
    """Return *permissions* de-duplicated and sorted, or raise 422 on anything outside the ceiling."""
    requested = {str(p) for p in permissions}
    allowed = {p.value for p in ANONYMOUS_PERMISSION_CEILING}
    unknown = sorted(requested - allowed)
    if unknown:
        raise AppError(
            code="validation_error",
            message=f"Anonymous visitors can only be granted {', '.join(sorted(allowed))}; got {', '.join(unknown)}",
            status_code=422,
            field="anonymous_permissions",
        )
    return sorted(requested)


async def set_anonymous_permissions(
    session: AsyncSession,
    project: Project,
    permissions: Iterable[str],
    actor: User,
    request: Request | None = None,
) -> Project:
    """Replace *project*'s anonymous permissions. Instance administrators only.

    Raises ``PermissionDeniedError`` for anyone else, including project
    managers, and a 422 ``project_not_public`` when a non-empty list is set on
    a project that is not public. A request that changes nothing writes no
    audit entry.
    """
    if not actor.is_admin:
        raise PermissionDeniedError("Only instance administrators can change anonymous access")

    new = normalize_anonymous_permissions(permissions)
    if new and not project.is_public:
        raise AppError(
            code="project_not_public",
            message=f"Project '{project.key}' is not public, so it cannot be opened to anonymous visitors",
            status_code=422,
            field="anonymous_permissions",
        )

    old = list(project.anonymous_permissions or [])
    if old == new:
        return project

    project.anonymous_permissions = new
    await session.flush()
    await _audit.log_project_anonymous_permissions_change(
        session=session,
        user_id=actor.id,
        project_id=project.id,
        project_key=project.key,
        old=old,
        new=new,
        reason=AnonymousPermissionsChangeReason.ADMIN_UPDATE,
        request=request,
    )
    return project


async def clear_anonymous_permissions_for_private(
    session: AsyncSession,
    project: Project,
    actor: User | None,
    request: Request | None = None,
) -> None:
    """Empty *project*'s anonymous permissions because it is being made private.

    Call it before ``is_public`` is set to false, in the same transaction, so
    the list is already empty when the project stops being public and
    ``ck_projects_anonymous_permissions_public`` holds at every flush. Does
    nothing, and audits nothing, when the list is already empty.
    """
    old = list(project.anonymous_permissions or [])
    if not old:
        return

    project.anonymous_permissions = []
    await _audit.log_project_anonymous_permissions_change(
        session=session,
        user_id=actor.id if actor is not None else None,
        project_id=project.id,
        project_key=project.key,
        old=old,
        new=[],
        reason=AnonymousPermissionsChangeReason.PROJECT_MADE_PRIVATE,
        request=request,
    )


async def list_projects_with_anonymous_permissions(session: AsyncSession) -> list[Project]:
    """Return every project that carries anonymous permissions, ordered by name.

    The predicate matches ``ix_projects_anonymous_readable``.
    """
    stmt = (
        select(Project)
        .where(Project.anonymous_permissions.op("<>")(text("'[]'::jsonb")))
        .order_by(Project.name, Project.id)
    )
    return list((await session.execute(stmt)).scalars().all())
