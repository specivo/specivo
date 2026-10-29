"""The project limit of the API key a request or MCP tool call authenticated with.

An API key may be limited to some projects: ``scopes = {"projects": [...]}``,
naming projects by key or by id. Such a key reaches those projects and nothing
else, whatever its owner may do elsewhere, admin or not.

How the limit is carried
------------------------
The limit is resolved to a set of project ids once per request (REST) or per
tool call (MCP), right after the key is authenticated
(:func:`activate_api_key_scope`), and held in a context variable for the rest
of that request or call. The access decisions read it from there instead of
taking it as an argument, so no call site can forget to pass it:

- ``permission_service.get_user_roles`` returns no roles in a project outside
  the limit, so ``check_permission``, issue visibility and time entry
  visibility deny there;
- ``check_permission`` and every admin shortcut honour the limit before the
  admin bypass (``admin_reaches``), and refuse project-less (global)
  permissions to a limited key;
- the SQL builders (``ProjectService.accessible_projects_clause``,
  ``IssueService.visible_issues_clause``, the time entry and search
  visibility) narrow to the limit (``scope_clause`` / ``scope_project_ids``);
- account-wide and instance-wide operations (API key management, sessions,
  password, the admin API) refuse a limited key (``require_unlimited_key``,
  ``is_global_admin``).

Anything outside the limit behaves as not found, exactly like a private
project the owner is not a member of.

The variable is reset around every HTTP request by
``ApiKeyScopeMiddleware`` and around every MCP tool call by the MCP session
helper, so a limit never outlives the call that authenticated it. Code that
runs outside a request (Celery tasks, the CLI) sees no limit.

Feature gate
------------
Whether a key *can* be limited is decided when it is created: without the
``api_key_scopes`` feature, ``ApiKeyService.create_key`` stores no scopes and
the key is unlimited. A limit that is stored is always enforced, even if the
feature is later switched off, so losing the feature never widens a key.

An empty or missing ``projects`` list means the key is not project-limited.
A list naming only projects that no longer exist leaves the key reaching
nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import false, or_, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from specivo.models.auth import ApiKey
    from specivo.models.user import User


@dataclass(frozen=True, slots=True)
class ProjectScope:
    """The projects a limited API key reaches, as resolved ids."""

    project_ids: frozenset[int]

    def allows(self, project_id: int | None) -> bool:
        return project_id is not None and project_id in self.project_ids


_current_scope: ContextVar[ProjectScope | None] = ContextVar("specivo_api_key_project_scope", default=None)


def current_project_scope() -> ProjectScope | None:
    """Return the project limit of the authenticating key, or None when there is none."""
    return _current_scope.get()


def project_in_scope(project_id: int | None) -> bool:
    """Return whether the current key may reach *project_id*.

    Always True without a limit. With one, a project-less (global) target is
    outside it.
    """
    scope = _current_scope.get()
    return scope is None or scope.allows(project_id)


def scope_clause(project_id_column: Any) -> ColumnElement[bool] | None:
    """Return a predicate limiting *project_id_column* to the current key's projects, or None without a limit."""
    scope = _current_scope.get()
    if scope is None:
        return None
    if not scope.project_ids:
        return false()
    clause: ColumnElement[bool] = project_id_column.in_(sorted(scope.project_ids))
    return clause


def scope_project_ids(project_id: int | None, project_ids: list[int] | None) -> tuple[int | None, list[int] | None]:
    """Narrow a ``(project_id, project_ids)`` pair, as the search services take it, to the current limit.

    Without a limit the pair is returned unchanged. With one, an explicit
    list is intersected with it, a single project outside it yields an empty
    list (no results), and no filter at all becomes the limit itself.
    """
    scope = _current_scope.get()
    if scope is None:
        return project_id, project_ids
    if project_ids is not None:
        return None, [pid for pid in project_ids if pid in scope.project_ids]
    if project_id is not None:
        return (project_id, None) if scope.allows(project_id) else (None, [])
    return None, sorted(scope.project_ids)


def is_global_admin(user: User) -> bool:
    """Return whether *user* holds instance-wide admin rights in this call.

    An admin authenticated with a project-limited key does not: its admin
    rights stop at the key's projects (``admin_reaches``).
    """
    return user.is_admin and _current_scope.get() is None


def admin_reaches(user: User, project_id: int | None) -> bool:
    """Return whether *user*'s admin bypass applies to *project_id* in this call."""
    return user.is_admin and project_in_scope(project_id)


def is_limited_key() -> bool:
    """Return whether the call authenticated with a project-limited key."""
    return _current_scope.get() is not None


def require_unlimited_key() -> None:
    """Refuse an account- or instance-wide operation to a project-limited key (403)."""
    if _current_scope.get() is not None:
        from specivo.core.exceptions import PermissionDeniedError
        from specivo.core.i18n import gettext as _

        raise PermissionDeniedError(_("This API key is limited to specific projects and cannot do this."))


# ---------------------------------------------------------------------------
# Resolution and binding
# ---------------------------------------------------------------------------


def _scope_entries(scopes: Any) -> list[Any] | None:
    """Return the raw ``projects`` entries of *scopes*, or None when the key is not limited."""
    if not scopes or not isinstance(scopes, dict):
        # No scopes, or a bare list of capability names (``["mcp"]``):
        # nothing limits the projects.
        return None
    raw = scopes.get("projects")
    if not raw:
        return None
    if isinstance(raw, str | int):
        return [raw]
    if isinstance(raw, list | tuple):
        return list(raw)
    # Unreadable limit: fail closed, the key reaches nothing.
    return []


async def resolve_project_scope(session: AsyncSession, scopes: Any) -> ProjectScope | None:
    """Resolve an API key's ``scopes`` JSON to a :class:`ProjectScope`, or None when the key is not limited.

    Entries name projects by key (case-insensitive) or by id; a string of
    digits is tried as both. Entries matching no project are dropped.
    """
    entries = _scope_entries(scopes)
    if entries is None:
        return None

    from specivo.models.project import Project

    ids: set[int] = set()
    keys: set[str] = set()
    for entry in entries:
        if isinstance(entry, bool):
            continue
        if isinstance(entry, int):
            ids.add(entry)
        elif isinstance(entry, str):
            value = entry.strip()
            if not value:
                continue
            keys.add(value.upper())
            if value.isdigit():
                ids.add(int(value))

    if not ids and not keys:
        return ProjectScope(frozenset())
    conditions = []
    if ids:
        conditions.append(Project.id.in_(sorted(ids)))
    if keys:
        conditions.append(Project.key.in_(sorted(keys)))
    found = (await session.execute(select(Project.id).where(or_(*conditions)))).scalars().all()
    return ProjectScope(frozenset(found))


async def activate_api_key_scope(session: AsyncSession, api_key: ApiKey) -> ProjectScope | None:
    """Resolve *api_key*'s project limit and make it the current one.

    Called right after the key authenticates. The caller's context must be
    reset afterwards (``ApiKeyScopeMiddleware``, ``bound_scope``).
    """
    scope = await resolve_project_scope(session, api_key.scopes)
    _current_scope.set(scope)
    return scope


def start_scope_context() -> Token[ProjectScope | None]:
    """Start a call with no limit; pass the token to :func:`end_scope_context`."""
    return _current_scope.set(None)


def end_scope_context(token: Token[ProjectScope | None]) -> None:
    """Restore the limit that was current before :func:`start_scope_context`."""
    _current_scope.reset(token)


@contextmanager
def bound_scope(scope: ProjectScope | None = None) -> Iterator[None]:
    """Run a block with *scope* as the current limit, restoring the previous one afterwards."""
    token = _current_scope.set(scope)
    try:
        yield
    finally:
        _current_scope.reset(token)
