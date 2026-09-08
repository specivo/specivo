"""Permission constants and check utilities.

A user reaches a project through one of two kinds of ``members`` row: one
held directly by the user, or one held by a user group the user belongs to.
Both kinds carry their roles the same way (via ``member_roles``), so every
membership read here resolves the union of the two.

- ``PERMISSIONS`` dict: canonical permission names + human labels.
- ``member_principal_clause(user_id)``: the single definition of "this
  ``members`` row grants *user_id* access" — direct row or group row. Reuse
  it anywhere membership is read for an access decision.
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
from enum import StrEnum
from typing import Any

from fastapi import Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from specivo.models.member import Member, MemberRole
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.user_group import UserGroupMember

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Membership predicate
# ---------------------------------------------------------------------------


def member_principal_clause(user_id: int) -> ColumnElement[bool]:
    """Return a predicate selecting the ``members`` rows that grant *user_id* access.

    A ``members`` row is held by exactly one principal: a user or a user
    group.  A row reaches *user_id* when it is the user's own row, or when it
    belongs to a group the user is in.  The ``OR`` keeps this to a single
    join-free predicate over ``members``, so the planner can still use
    ``ix_members_user_id`` / ``ix_members_group_id``.

    This is the one definition of "is this user a member"; use it for every
    membership read that feeds an access decision.  Membership rows looked up
    to be *edited* (add/update/remove a specific user's roles) must stay
    user-only and should not use this.
    """
    users_groups = select(UserGroupMember.group_id).where(UserGroupMember.user_id == user_id)
    return or_(Member.user_id == user_id, Member.group_id.in_(users_groups))


# ---------------------------------------------------------------------------
# Role lookup (cacheable per request)
# ---------------------------------------------------------------------------

# Per-session role cache to avoid repeated JOINs within the same request/tool
# call.  Keyed by (user_id, project_id) → list[Role]: group-held roles are
# already folded into that list, so the key stays the user, not the principal.
# Callers should call ``get_user_roles()`` instead of querying directly.
_role_cache: dict[tuple[int, int], list[Role]] = {}


def clear_role_cache() -> None:
    """Clear the in-process role cache. Call at request boundaries."""
    _role_cache.clear()


async def get_user_roles(
    session: AsyncSession,
    user_id: int,
    project_id: int,
) -> list[Role]:
    """Return roles for *user_id* on *project_id*, with per-request caching.

    The result is the union of the roles held by the user's own membership
    row and those held by every group the user belongs to; a user who is both
    a direct member and in a group ends up holding both sets.  Roles are
    deduplicated, so two groups granting the same role yield it once.

    The JOIN (roles → member_roles → members) is the most frequent query in
    the system.  This function caches the result so repeated checks within
    the same request hit the DB only once.
    """
    cache_key = (user_id, project_id)
    if cache_key in _role_cache:
        return _role_cache[cache_key]

    stmt = (
        select(Role)
        .join(MemberRole, MemberRole.role_id == Role.id)
        .join(Member, Member.id == MemberRole.member_id)
        .where(Member.project_id == project_id, member_principal_clause(user_id))
        .distinct()
    )
    roles = list((await session.execute(stmt)).scalars().all())
    _role_cache[cache_key] = roles
    return roles


# ---------------------------------------------------------------------------
# Permission catalogue
# ---------------------------------------------------------------------------


class Permission(StrEnum):
    """Canonical permission keys.

    Subclassing ``StrEnum`` keeps each member's string value identical to its
    name, so members are drop-in replacements for the raw strings stored in
    ``roles.permissions`` JSONB and accepted by ``check_permission()``.
    Prefer using these constants over string literals at call sites.
    """

    # --- Issues ---
    ADD_ISSUES = "add_issues"
    EDIT_ISSUES = "edit_issues"
    DELETE_ISSUES = "delete_issues"
    ADD_ISSUE_NOTES = "add_issue_notes"
    EDIT_OWN_NOTES = "edit_own_notes"
    EDIT_NOTES = "edit_notes"
    DELETE_OWN_NOTES = "delete_own_notes"
    DELETE_NOTES = "delete_notes"
    MANAGE_ISSUE_RELATIONS = "manage_issue_relations"
    MANAGE_SUBTASKS = "manage_subtasks"
    VIEW_ISSUES = "view_issues"
    VIEW_PRIVATE_NOTES = "view_private_notes"
    SET_ISSUES_PRIVATE = "set_issues_private"
    # --- Project management ---
    MANAGE_MEMBERS = "manage_members"
    MANAGE_VERSIONS = "manage_versions"
    MANAGE_SPRINTS = "manage_sprints"
    MANAGE_RECURRING_TASKS = "manage_recurring_tasks"
    VIEW_WIKI = "view_wiki"
    MANAGE_WIKI = "manage_wiki"
    DELETE_WIKI_PAGES = "delete_wiki_pages"
    # --- Time tracking ---
    VIEW_TIME_ENTRIES = "view_time_entries"
    LOG_TIME = "log_time"
    MANAGE_TIME_ENTRIES = "manage_time_entries"
    # --- Admin ---
    MANAGE_PROJECT = "manage_project"


# Human-readable labels for the admin role editor. Keyed by the enum's string
# value so existing consumers (templates, JSONB lookups) keep working unchanged.
PERMISSIONS: dict[str, str] = {
    Permission.ADD_ISSUES: "Create issues",
    Permission.EDIT_ISSUES: "Edit issues",
    Permission.DELETE_ISSUES: "Delete issues",
    Permission.ADD_ISSUE_NOTES: "Add comments",
    Permission.EDIT_OWN_NOTES: "Edit own comments",
    Permission.EDIT_NOTES: "Edit any comments",
    Permission.DELETE_OWN_NOTES: "Delete own comments",
    Permission.DELETE_NOTES: "Delete any comments",
    Permission.MANAGE_ISSUE_RELATIONS: "Manage issue relations",
    Permission.MANAGE_SUBTASKS: "Manage subtasks",
    Permission.VIEW_ISSUES: "View issues",
    Permission.VIEW_PRIVATE_NOTES: "View private notes",
    Permission.SET_ISSUES_PRIVATE: "Set issues private",
    Permission.MANAGE_MEMBERS: "Manage project members",
    Permission.MANAGE_VERSIONS: "Manage versions",
    Permission.MANAGE_SPRINTS: "Manage sprints (create, start, complete, edit, delete)",
    Permission.MANAGE_RECURRING_TASKS: "Manage recurring tasks (create, edit, skip, delete)",
    Permission.VIEW_WIKI: "View wiki pages",
    Permission.MANAGE_WIKI: "Manage wiki pages",
    Permission.DELETE_WIKI_PAGES: "Delete wiki pages",
    Permission.VIEW_TIME_ENTRIES: "View time entries",
    Permission.LOG_TIME: "Log time",
    Permission.MANAGE_TIME_ENTRIES: "Edit/delete any time entries",
    Permission.MANAGE_PROJECT: "Manage project settings",
}


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------


async def check_permission(
    user: User,
    project_id: int | None,
    permission: str | Permission,
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
