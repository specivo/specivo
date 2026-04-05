"""Permission constants and check utilities.

- ``PERMISSIONS`` dict: canonical permission names + human labels.
- ``check_permission(user, project_id, permission, session)``:
  - Admin users always pass.
  - For non-admins: queries member_roles + roles for this user+project,
    and checks whether any role grants the requested permission or ``"*"``.
- ``check_permission()`` async function for endpoint-level authorization.
- ``get_user_roles(session, user_id, project_id)``: cacheable role lookup
  used by both permission checks and visibility checks.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member, MemberRole
from specivo.models.role import Role
from specivo.models.user import User

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Role lookup (cacheable per request)
# ---------------------------------------------------------------------------

# Per-session role cache to avoid repeated 3-table JOINs within the same
# request/tool call.  Keyed by (user_id, project_id) → list[Role].
# Callers should call ``get_user_roles()`` instead of querying directly.
_role_cache: dict[tuple[int, int], list[Role]] = {}


def clear_role_cache() -> None:
    """Clear the in-process role cache. Call at request boundaries."""
    _role_cache.clear()


async def get_user_roles(
    session: AsyncSession, user_id: int, project_id: int,
) -> list[Role]:
    """Return roles for *user_id* on *project_id*, with per-request caching.

    The 3-table JOIN (roles → member_roles → members) is the most
    frequent query in the system.  This function caches the result so
    repeated checks within the same request hit the DB only once.
    """
    cache_key = (user_id, project_id)
    if cache_key in _role_cache:
        return _role_cache[cache_key]

    stmt = (
        select(Role)
        .join(MemberRole, MemberRole.role_id == Role.id)
        .join(Member, Member.id == MemberRole.member_id)
        .where(Member.project_id == project_id, Member.user_id == user_id)
    )
    roles = list((await session.execute(stmt)).scalars().all())
    _role_cache[cache_key] = roles
    return roles

# ---------------------------------------------------------------------------
# Permission catalogue
# ---------------------------------------------------------------------------

PERMISSIONS: dict[str, str] = {
    # --- Issues ---
    "add_issues": "Create issues",
    "edit_issues": "Edit issues",
    "delete_issues": "Delete issues",
    "add_issue_notes": "Add comments",
    "edit_own_notes": "Edit own comments",
    "edit_notes": "Edit any comments",
    "delete_own_notes": "Delete own comments",
    "delete_notes": "Delete any comments",
    "manage_issue_relations": "Manage issue relations",
    "manage_subtasks": "Manage subtasks",
    "view_issues": "View issues",
    "view_private_notes": "View private notes",
    "set_issues_private": "Set issues private",
    # --- Project management ---
    "manage_members": "Manage project members",
    "manage_versions": "Manage versions",
    "view_wiki": "View wiki pages",
    "manage_wiki": "Manage wiki pages",
    # --- Time tracking ---
    "view_time_entries": "View time entries",
    "log_time": "Log time",
    "manage_time_entries": "Edit/delete any time entries",
    # --- Admin ---
    "manage_project": "Manage project settings",
}


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------


async def check_permission(
    user: User,
    project_id: int | None,
    permission: str,
    session: AsyncSession,
    api_key_scopes: dict | None = None,
    request: Request | None = None,
) -> bool:
    """Return ``True`` if *user* holds *permission*.

    Resolution order:
    1. Admins always have all permissions.
    2. API key scope check — if the key has scoped ``projects``, the
       project must be in the allowed list.
    3. Project-scoped member role lookup.
    4. Fallback: ``False``.

    ``api_key_scopes`` is the ``scopes`` JSONB from the authenticating API key
    (``None`` when authenticated via JWT or when the key has no scope restrictions).
    """
    if user.is_admin:
        return True

    if project_id is None:
        return False

    # API key scope enforcement: check that the project is in the allowed list
    if api_key_scopes and api_key_scopes.get("projects"):
        allowed_projects = api_key_scopes["projects"]
        # Scopes may contain project keys (strings) or project IDs (ints)
        from specivo.models.project import Project

        project_result = await session.execute(select(Project.key).where(Project.id == project_id))
        project_key = project_result.scalar_one_or_none()
        # Check both numeric ID and string key
        if project_id not in allowed_projects and str(project_id) not in [str(p) for p in allowed_projects]:
            if project_key is None or project_key not in allowed_projects:
                return False

    # Project-scoped member role lookup (cached per request)
    roles = await get_user_roles(session, user.id, project_id)
    granted = _any_role_grants(roles, permission)

    # Audit logging (non-critical — never block permission checks).
    # Events are collected in request.state.audit_events for batch INSERT
    # by AuditBatchMiddleware after the response. The middleware uses its
    # own session, so events survive outer transaction rollbacks (e.g. 403).
    if request is not None:
        try:
            from specivo.services.security_audit_service import SecurityAuditService

            audit = SecurityAuditService()
            if granted:
                await audit.log_access_granted(
                    session=session,
                    user_id=user.id,
                    request=request,
                    project_id=project_id,
                    permission=permission,
                )
            else:
                await audit.log_access_denied(
                    session=session,
                    user_id=user.id,
                    request=request,
                    project_id=project_id,
                    permission=permission,
                )
        except Exception:
            logger.warning("Security audit logging failed", exc_info=True)

    return granted


def _role_grants(permissions_list: list[Any], permission: str) -> bool:
    """Return ``True`` if *permissions_list* grants *permission* or ``"*"``."""
    return "*" in permissions_list or permission in permissions_list


def _any_role_grants(roles: list[Role], permission: str) -> bool:
    """Return ``True`` if any role in *roles* grants *permission* or ``"*"``."""
    return any(_role_grants(role.permissions, permission) for role in roles)
