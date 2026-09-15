"""Anonymous read access: the per-project opt-in and the instance switch.

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

The instance switch is the ``anonymous_access_enabled`` setting. It is off by
default, and off whenever the row is absent. Turning it on exposes nothing by
itself: a project can only ever be read without an account while the switch
is on and the project is opted in. Only instance administrators change it,
turning it on must name exactly the projects opted in at that moment, and
every change is audited. It is read from the database on every call, so a
change applies to the next request without any process cache to invalidate.

Both kinds of change take ``ANONYMOUS_ACCESS_LOCK_KEY``, a transaction-scoped
advisory lock. Turning the switch on therefore cannot interleave with a
project being opted in or out: the confirmed list is compared with the
opted-in projects, and the switch written, while no opt-in can change.

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
from specivo.models.setting import Setting
from specivo.models.user import User
from specivo.services.permission_service import Permission
from specivo.services.security_audit_service import SecurityAuditService
from specivo.services.settings_service import SettingsService

# The only permissions an anonymous visitor can ever hold. Mirrors
# ck_projects_anonymous_permissions_allowed.
ANONYMOUS_PERMISSION_CEILING: frozenset[Permission] = frozenset({Permission.VIEW_ISSUES, Permission.VIEW_WIKI})

# The instance switch. Only the exact value "true" turns it on.
ANONYMOUS_ACCESS_SETTING_KEY = "anonymous_access_enabled"
_ENABLED_VALUE = "true"
_DISABLED_VALUE = "false"

_audit = SecurityAuditService()
_settings = SettingsService()

# Transaction-scoped advisory lock serialising the instance switch with
# per-project opt-in changes. The value is arbitrary; the other advisory locks
# in the codebase key on issue and tree-root ids, far below it.
ANONYMOUS_ACCESS_LOCK_KEY = 0x5350_414E_4F4E_0001


async def lock_anonymous_access(session: AsyncSession) -> None:
    """Wait until no other transaction is changing anonymous access, then hold the lock until this one ends."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ANONYMOUS_ACCESS_LOCK_KEY})


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

    # Serialise with the instance switch, then re-read the row so the check
    # and the audited old value reflect anything committed while waiting.
    await lock_anonymous_access(session)
    await session.refresh(project, attribute_names=["is_public", "anonymous_permissions"])
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
    await lock_anonymous_access(session)
    await session.refresh(project, attribute_names=["anonymous_permissions"])
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


# ---------------------------------------------------------------------------
# Instance switch
# ---------------------------------------------------------------------------


async def is_anonymous_access_enabled(session: AsyncSession) -> bool:
    """Return True only if the ``anonymous_access_enabled`` setting is exactly ``"true"``.

    An absent row, NULL or any other value means off. The value is read from
    the database on every call, so a change is seen by the next request.
    """
    value = await session.scalar(select(Setting.value).where(Setting.key == ANONYMOUS_ACCESS_SETTING_KEY))
    return value == _ENABLED_VALUE


def anonymous_access_project_summary(project: Project) -> dict[str, object]:
    """Return the fields that name an opted-in project in a confirmation or audit entry."""
    return {
        "key": project.key,
        "name": project.name,
        "anonymous_permissions": list(project.anonymous_permissions or []),
    }


class AnonymousAccessConfirmationError(AppError):
    """Turning the switch on was not confirmed against the projects opted in now (409).

    ``projects`` holds the projects opted in at this moment, and
    ``details.projects`` carries the same list for API clients.
    """

    def __init__(self, code: str, message: str, projects: list[Project]) -> None:
        super().__init__(
            code=code,
            message=message,
            status_code=409,
            details={"projects": [anonymous_access_project_summary(p) for p in projects]},
        )
        self.projects = projects


class AnonymousAccessConfirmationRequiredError(AnonymousAccessConfirmationError):
    """Turning the switch on named no projects at all."""

    def __init__(self, projects: list[Project]) -> None:
        super().__init__(
            "confirmation_required",
            f"{len(projects)} project(s) are opted in to anonymous reading. To turn anonymous access on, "
            "repeat the request with confirmed_projects set to exactly their keys.",
            projects,
        )


class AnonymousAccessConfirmationStaleError(AnonymousAccessConfirmationError):
    """Turning the switch on named a different set of projects than is opted in now."""

    def __init__(self, projects: list[Project]) -> None:
        super().__init__(
            "confirmation_stale",
            "The projects opted in to anonymous reading changed after they were confirmed. "
            "Review the current list and confirm again.",
            projects,
        )


async def set_anonymous_access_enabled(
    session: AsyncSession,
    enabled: bool,
    actor: User,
    *,
    confirmed_projects: Iterable[str] | None = None,
    request: Request | None = None,
) -> bool:
    """Turn the instance switch on or off. Instance administrators only.

    Turning it on requires *confirmed_projects*, the keys of the projects the
    administrator was shown. As a set it must equal the projects opted in at
    the moment of the change, and it may be empty. ``None`` raises
    ``AnonymousAccessConfirmationRequiredError``; a different set raises
    ``AnonymousAccessConfirmationStaleError``. Both carry the current list.
    Turning it off needs no confirmation. A request that changes nothing
    writes nothing.

    The comparison and the write happen under ``ANONYMOUS_ACCESS_LOCK_KEY``,
    which every per-project opt-in change also takes, so the opted-in set
    cannot change between them. Every change is audited with the old and new
    value, the opted-in project keys and, when turning on, the confirmed keys.
    """
    if not actor.is_admin:
        raise PermissionDeniedError("Only instance administrators can change anonymous access")

    await lock_anonymous_access(session)
    old = await is_anonymous_access_enabled(session)
    if old == enabled:
        return enabled

    projects = await list_projects_with_anonymous_permissions(session)
    opted_in = sorted(p.key for p in projects)
    confirmed: list[str] | None = None
    if enabled:
        if confirmed_projects is None:
            raise AnonymousAccessConfirmationRequiredError(projects)
        confirmed = sorted({key.strip().upper() for key in confirmed_projects})
        if confirmed != opted_in:
            raise AnonymousAccessConfirmationStaleError(projects)

    await _settings.set_many(session, {ANONYMOUS_ACCESS_SETTING_KEY: _ENABLED_VALUE if enabled else _DISABLED_VALUE})
    await _audit.log_anonymous_access_switch_change(
        session=session,
        user_id=actor.id,
        setting_key=ANONYMOUS_ACCESS_SETTING_KEY,
        old=old,
        new=enabled,
        opted_in_projects=opted_in,
        confirmed_projects=confirmed,
        request=request,
    )
    return enabled
