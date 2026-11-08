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

Creation
--------
A limit is validated once, when the key is created
(``ApiKeyService.create_key``): it must be ``{"projects": [...]}`` naming
1 to 200 projects, and every entry must name an existing project, one the
owner can see for a self-service key (:func:`resolve_scope_for_creation`).
It is stored as ``{"projects": [ids]}``, so renaming a project key does not
change what the key reaches. The limit stays fixed after creation.

Reading stays tolerant: limits stored by key (before creation stored ids)
still resolve, a bare list (``["mcp"]``) or JSON ``null`` means the key is
not limited, and an unreadable ``projects`` value fails closed. A stored
limit is always enforced; it must never fail open or crash.

A list naming only projects that no longer exist leaves the key reaching
nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import false, or_, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from specivo.models.auth import ApiKey
    from specivo.models.user import User


@dataclass(frozen=True, slots=True)
class ProjectScope:
    """The projects a limited API key reaches, as resolved ids.

    ``key_id`` is the limited key itself, when known (set by
    :func:`activate_api_key_scope`): rows that belong to no project, such as
    unassigned AI usage entries, are reachable only where that key wrote them.
    """

    project_ids: frozenset[int]
    key_id: int | None = None

    def allows(self, project_id: int | None) -> bool:
        return project_id is not None and project_id in self.project_ids


_current_scope: ContextVar[ProjectScope | None] = ContextVar("specivo_api_key_project_scope", default=None)


def current_project_scope() -> ProjectScope | None:
    """Return the project limit of the authenticating key, or None when there is none."""
    return _current_scope.get()


def limited_key_id() -> int | None:
    """Return the id of the authenticating project-limited key, or None (no limit, or the key is unknown)."""
    scope = _current_scope.get()
    return scope.key_id if scope is not None else None


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

#: Largest value ``projects.id`` can hold; binding a larger one makes the database refuse the query.
_MAX_PROJECT_ID = 2**31 - 1


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


def _usable_id(value: int) -> set[int]:
    """*value* as a one-item set, or empty when no project can have it (the id column is 32 bits)."""
    return {value} if 0 < value <= _MAX_PROJECT_ID else set()


def _entry_refs(entry: Any) -> tuple[set[int], set[str]]:
    """The project ids and upper-cased keys one ``projects`` entry may name.

    An int is an id; a string is a key (case-insensitive), and a string of
    digits is tried as an id too. Anything else names nothing.
    """
    if isinstance(entry, bool):
        return set(), set()
    if isinstance(entry, int):
        return _usable_id(entry), set()
    if isinstance(entry, str):
        value = entry.strip()
        if not value:
            return set(), set()
        # isdecimal alone accepts other scripts' digits; "²" passes isdigit but breaks int().
        ids = _usable_id(int(value)) if value.isascii() and value.isdecimal() else set()
        return ids, {value.upper()}
    return set(), set()


async def _match_projects(
    session: AsyncSession, entries: list[Any], extra: ColumnElement[bool] | None = None
) -> list[tuple[int, str]]:
    """Return ``(id, key)`` of every project some entry names, further narrowed by *extra*."""
    from specivo.models.project import Project

    ids: set[int] = set()
    keys: set[str] = set()
    for entry in entries:
        entry_ids, entry_keys = _entry_refs(entry)
        ids |= entry_ids
        keys |= entry_keys
    if not ids and not keys:
        return []
    conditions = []
    if ids:
        conditions.append(Project.id.in_(sorted(ids)))
    if keys:
        conditions.append(Project.key.in_(sorted(keys)))
    stmt = select(Project.id, Project.key).where(or_(*conditions))
    if extra is not None:
        stmt = stmt.where(extra)
    return [(row.id, row.key) for row in (await session.execute(stmt)).all()]


async def resolve_project_scope(session: AsyncSession, scopes: Any) -> ProjectScope | None:
    """Resolve an API key's ``scopes`` JSON to a :class:`ProjectScope`, or None when the key is not limited.

    Entries name projects by key (case-insensitive) or by id; a string of
    digits is tried as both. Entries matching no project are dropped.
    """
    entries = _scope_entries(scopes)
    if entries is None:
        return None
    found = await _match_projects(session, entries)
    return ProjectScope(frozenset(pid for pid, _key in found))


class UnknownScopeProjectError(LookupError):
    """A ``projects`` entry names no project the key may be limited to; ``entry`` is the entry as given."""

    def __init__(self, entry: str | int) -> None:
        super().__init__(entry)
        self.entry = entry


async def resolve_scope_for_creation(
    session: AsyncSession, entries: list[str | int], owner: User | None, *, check_access: bool
) -> list[int]:
    """Resolve the ``projects`` entries of a new key's limit to sorted, distinct project ids.

    Every entry must name an existing project; with *check_access*, also one
    *owner* can see (``ProjectService.accessible_projects_clause``). A missing
    project and one the owner cannot see raise the same
    :class:`UnknownScopeProjectError`, so the refusal reveals nothing.
    """
    extra: ColumnElement[bool] | None = None
    if check_access:
        if owner is None:
            raise UnknownScopeProjectError(entries[0])
        from specivo.services.project_service import ProjectService

        extra = await ProjectService().accessible_projects_clause(session, owner)
    found = await _match_projects(session, list(entries), extra)
    by_id = {pid: pid for pid, _key in found}
    by_key = {key.upper(): pid for pid, key in found}
    resolved: set[int] = set()
    for entry in entries:
        entry_ids, entry_keys = _entry_refs(entry)
        hit = next((by_key[k] for k in entry_keys if k in by_key), None)
        if hit is None:
            hit = next((by_id[i] for i in entry_ids if i in by_id), None)
        if hit is None:
            raise UnknownScopeProjectError(entry)
        resolved.add(hit)
    return sorted(resolved)


async def project_keys_by_api_key(session: AsyncSession, keys: list[ApiKey]) -> dict[int, list[str] | None]:
    """Map each key's id to the sorted keys of the projects it is limited to (None: not limited), in one query.

    A project that no longer exists is left out, so a key whose projects are
    all gone maps to an empty list: it reaches nothing.
    """
    entries_by_key = {key.id: _scope_entries(key.scopes) for key in keys}
    all_entries = [entry for entries in entries_by_key.values() if entries for entry in entries]
    found = await _match_projects(session, all_entries) if all_entries else []
    key_by_id = dict(found)
    id_by_key = {key.upper(): pid for pid, key in found}
    result: dict[int, list[str] | None] = {}
    for key_id, entries in entries_by_key.items():
        if entries is None:
            result[key_id] = None
            continue
        names: set[str] = set()
        for entry in entries:
            entry_ids, entry_keys = _entry_refs(entry)
            names |= {key_by_id[i] for i in entry_ids if i in key_by_id}
            names |= {key_by_id[id_by_key[k]] for k in entry_keys if k in id_by_key}
        result[key_id] = sorted(names)
    return result


async def activate_api_key_scope(session: AsyncSession, api_key: ApiKey) -> ProjectScope | None:
    """Resolve *api_key*'s project limit and make it the current one.

    Called right after the key authenticates. The caller's context must be
    reset afterwards (``ApiKeyScopeMiddleware``, ``bound_scope``).
    """
    scope = await resolve_project_scope(session, api_key.scopes)
    if scope is not None:
        scope = replace(scope, key_id=api_key.id)
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
