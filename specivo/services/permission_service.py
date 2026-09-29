"""Permission catalogue, role resolution and the permission check.

Who may do what in a project is decided here and nowhere else.
``get_user_roles`` resolves the roles a user holds in a project, and every
access decision derives from it: ``check_permission``, issue visibility in
``IssueService`` and, mirrored in SQL, the search visibility CTE.

Role resolution
---------------
- **Member** — a ``members`` row held by the user, or by a user group the user
  belongs to (``member_principal_clause``): the roles on those rows and
  nothing else. A membership *replaces* the fallback below, even when its
  roles grant nothing.
- **Signed-in user without a membership, on a public project** — the seeded
  Non member role (``roles.builtin = 1``), plus the project's anonymous role
  when it applies, so a signed-in user never sees less than an anonymous
  visitor would.
- **The anonymous user** — only the project's anonymous role.
- **Anything else** (a private project, no membership) — no roles.

The *anonymous role* is transient. It is built from
``projects.anonymous_permissions`` capped at ``ANONYMOUS_PERMISSION_CEILING``,
and exists only while the instance switch is on, the project is public and
active, and the capped list is not empty. No ``roles`` row stands for it.

``check_permission`` additionally denies the anonymous user anything outside
the ceiling before it looks at a single role.

Caching
-------
Membership roles and the Non member role are cached in ``session.info``, so
the cache lives exactly as long as the request's (or MCP tool call's)
session and is never shared between concurrent requests. Anonymous access
and the instance switch are never cached: a change applies to the next call.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from fastapi import Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from specivo.models.member import Member, MemberRole
from specivo.models.project import PROJECT_STATUS_ACTIVE, Project
from specivo.models.role import Role, RoleBuiltin
from specivo.models.user import User
from specivo.models.user_group import UserGroupMember

logger = logging.getLogger(__name__)


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

# The only permissions the anonymous user can ever hold. Mirrors
# ck_projects_anonymous_permissions_allowed, and is enforced again inside
# check_permission whatever a project's JSON says.
ANONYMOUS_PERMISSION_CEILING: frozenset[Permission] = frozenset({Permission.VIEW_ISSUES, Permission.VIEW_WIKI})

_CEILING_VALUES: frozenset[str] = frozenset(p.value for p in ANONYMOUS_PERMISSION_CEILING)

# Display name of the transient role an anonymous visitor holds.
ANONYMOUS_ROLE_NAME = "Anonymous"


def role_grants(permissions: Collection[Any], permission: str) -> bool:
    """Return ``True`` if *permissions* grants *permission*, directly or through ``"*"``."""
    return "*" in permissions or permission in permissions


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
# Role resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedRole:
    """A role as resolved for one user in one project.

    A detached snapshot rather than an ORM ``Role``: it is cached in
    ``session.info`` and must stay readable after the session commits, rolls
    back or expires its instances. The anonymous role has no row, so its
    ``id`` is None and ``transient`` is True.
    """

    id: int | None
    name: str
    permissions: frozenset[str]
    issues_visibility: str
    builtin: int = RoleBuiltin.CUSTOM
    transient: bool = False

    @classmethod
    def of(cls, role: Role) -> ResolvedRole:
        return cls(
            id=role.id,
            name=role.name,
            permissions=frozenset(role.permissions or ()),
            issues_visibility=role.issues_visibility,
            builtin=role.builtin,
        )

    def grants(self, permission: str) -> bool:
        return role_grants(self.permissions, permission)


_ROLE_CACHE_KEY = "specivo.role_cache"
_NON_MEMBER_CACHE_KEY = "non_member"


def _role_cache(session: AsyncSession) -> dict[Any, Any]:
    cache: dict[Any, Any] = session.info.setdefault(_ROLE_CACHE_KEY, {})
    return cache


def clear_role_cache(session: AsyncSession) -> None:
    """Forget the roles resolved through *session*.

    Only needed when a session changes memberships or roles and then asks
    again; each request and MCP tool call starts with a fresh session and so
    with an empty cache.
    """
    session.info.pop(_ROLE_CACHE_KEY, None)


async def _membership_roles(session: AsyncSession, user: User, project: Project) -> tuple[ResolvedRole, ...] | None:
    """Return the roles *user*'s membership rows carry on *project*, or None without a membership row.

    The union of the user's own row and every row held by a group they are in,
    deduplicated. A membership row that carries no roles still counts as a
    membership and yields an empty tuple, not None.
    """
    cache = _role_cache(session)
    key = ("member", user.id, project.id)
    if key in cache:
        cached: tuple[ResolvedRole, ...] | None = cache[key]
        return cached

    rows = (
        await session.execute(
            select(Member.id, Role)
            .select_from(Member)
            .outerjoin(MemberRole, MemberRole.member_id == Member.id)
            .outerjoin(Role, Role.id == MemberRole.role_id)
            .where(Member.project_id == project.id, member_principal_clause(user.id))
        )
    ).all()

    result: tuple[ResolvedRole, ...] | None
    if not rows:
        result = None
    else:
        by_id: dict[int, ResolvedRole] = {}
        for _member_id, role in rows:
            if role is not None and role.id not in by_id:
                by_id[role.id] = ResolvedRole.of(role)
        result = tuple(by_id.values())
    cache[key] = result
    return result


async def get_non_member_role(session: AsyncSession) -> ResolvedRole | None:
    """Return the seeded Non member role, cached per session.

    Returns None, with a warning, only when the row is missing, which means the
    database was not migrated.
    """
    cache = _role_cache(session)
    if _NON_MEMBER_CACHE_KEY not in cache:
        role = (await session.execute(select(Role).where(Role.builtin == RoleBuiltin.NON_MEMBER))).scalar_one_or_none()
        if role is None:
            logger.warning("The Non member role is missing; run the database migrations to restore it")
        cache[_NON_MEMBER_CACHE_KEY] = ResolvedRole.of(role) if role is not None else None
    non_member: ResolvedRole | None = cache[_NON_MEMBER_CACHE_KEY]
    return non_member


async def anonymous_role(session: AsyncSession, project: Project) -> ResolvedRole | None:
    """Return *project*'s transient anonymous role, or None when anonymous access does not apply there.

    It applies only while the instance switch is on, the project is public and
    active, and its anonymous permissions, capped at the ceiling, are not
    empty. Never cached: the switch is read from the database on every call.
    """
    permissions = frozenset(project.anonymous_permissions or ()) & _CEILING_VALUES
    if not permissions or not project.is_public or project.status != PROJECT_STATUS_ACTIVE:
        return None

    from specivo.services.anonymous_access_service import is_anonymous_access_enabled

    if not await is_anonymous_access_enabled(session):
        return None
    return ResolvedRole(
        id=None,
        name=ANONYMOUS_ROLE_NAME,
        permissions=permissions,
        issues_visibility="default",
        transient=True,
    )


async def get_user_roles(session: AsyncSession, user: User, project: Project) -> list[ResolvedRole]:
    """Return the roles *user* holds in *project*, following the rules in the module docstring.

    Admins are resolved like anyone else; callers short-circuit them.
    """
    if user.is_anonymous:
        anonymous = await anonymous_role(session, project)
        return [anonymous] if anonymous is not None else []

    membership = await _membership_roles(session, user, project)
    if membership is not None:
        return list(membership)

    if not project.is_public:
        return []

    roles: list[ResolvedRole] = []
    non_member = await get_non_member_role(session)
    if non_member is not None:
        roles.append(non_member)
    anonymous = await anonymous_role(session, project)
    if anonymous is not None:
        roles.append(anonymous)
    return roles


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
    """Return ``True`` if *user* holds *permission* in the project.

    Resolution order:
    1. The anonymous user is denied anything outside
       ``ANONYMOUS_PERMISSION_CEILING``, before anything else is consulted.
    2. Admins always have all permissions.
    3. API key scope check — if the key has scoped ``projects``, the
       project must be in the allowed list.
    4. Any role from ``get_user_roles`` granting *permission* or ``"*"``.

    ``api_key_scopes`` is the ``scopes`` JSONB from the authenticating API key
    (``None`` when authenticated via JWT or when the key has no scope restrictions).
    """
    if user.is_anonymous and permission not in ANONYMOUS_PERMISSION_CEILING:
        return False

    if user.is_admin:
        return True

    if project_id is None:
        return False

    project = await session.get(Project, project_id)

    # API key scope enforcement: check that the project is in the allowed list
    if api_key_scopes and api_key_scopes.get("projects"):
        allowed_projects = api_key_scopes["projects"]
        # Scopes may contain project keys (strings) or project IDs (ints)
        if project_id not in allowed_projects and str(project_id) not in [str(p) for p in allowed_projects]:
            if project is None or project.key not in allowed_projects:
                return False

    if project is None:
        return False

    roles = await get_user_roles(session, user, project)
    granted = any(role.grants(permission) for role in roles)

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
