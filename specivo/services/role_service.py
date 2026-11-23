"""Role administration: list, create (blank or as a copy), edit, reorder and delete roles.

Roles are global: a role means the same thing in every project it is
assigned in, so editing one is admin work and happens only here. What each
permission means, which ones need which, and what the builtin Non member
role may hold is ``permission_catalogue``; this service applies it.

Worth knowing before reading further:

- **Names** are unique case-insensitively here (409 ``role_name_taken``),
  and may not be the transient anonymous role's name. The database only
  enforces exact uniqueness, as the backstop.
- **Builtin roles** (Non member) keep their name and are never assignable;
  the database refuses to delete them. Their permissions are editable up to
  the ``NON_MEMBER_ALLOWED`` ceiling, refused with 422 rather than stripped.
- **System roles** (``system_key`` set, the Automation role) can be renamed
  and edited but not deleted, and the key is never copied.
- **Edits** carry ``lock_version``. Reordering writes ``position`` with Core
  ``UPDATE`` statements, so it never bumps the version and never breaks an
  editor someone has open.
- **Delete** is refused while any project membership holds the role. The
  role's workflow rows go with it through the foreign key cascade.
- Nothing about roles is cached beyond the session (``clear_role_cache``),
  except workflow transitions in Redis, which a delete invalidates.

The audit events are written by the REST router from what these methods
return, so each write method hands back the facts the event needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from specivo.core.exceptions import AppError, ConflictError, NotFoundError, ValidationError
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role, RoleBuiltin
from specivo.models.workflow import WorkflowFieldRule, WorkflowTransition
from specivo.schemas.role import RoleCreate, RoleUpdate
from specivo.services.permission_catalogue import FULL_ACCESS, normalise_permissions, not_allowed_for_non_member
from specivo.services.permission_service import ANONYMOUS_ROLE_NAME, clear_role_cache
from specivo.services.workflow_service import WorkflowService

_VISIBILITY_FIELDS: tuple[str, ...] = ("users_visibility", "time_entries_visibility", "ai_usage_visibility")
_COPIED_VISIBILITY: tuple[str, ...] = ("issues_visibility", *_VISIBILITY_FIELDS)


class RoleNameTakenError(AppError):
    def __init__(self, name: str) -> None:
        super().__init__(
            code="role_name_taken",
            message=f"A role named '{name}' already exists",
            status_code=409,
            field="name",
        )


class RoleInUseError(AppError):
    def __init__(self, usage: RoleUsage) -> None:
        super().__init__(
            code="role_in_use",
            message=f"The role is used by {usage.membership_count} project membership(s); remove it from them first",
            status_code=409,
            details={"membership_count": usage.membership_count, "projects": usage.projects},
        )


class RoleProtectedError(AppError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(code="role_protected", message=message, status_code=409, details={"reason": reason})


class RolePermissionNotAllowedError(AppError):
    def __init__(self, not_allowed: list[str]) -> None:
        super().__init__(
            code="role_permission_not_allowed",
            message=f"The Non member role cannot hold: {', '.join(not_allowed)}",
            status_code=422,
            field="permissions",
            details={"not_allowed": not_allowed},
        )


@dataclass(frozen=True, slots=True)
class RoleUsage:
    """Where a role is used: how many memberships hold it, and on which projects (up to a limit)."""

    membership_count: int
    projects: list[dict[str, Any]] = field(default_factory=list)


def _usage_rows(role_id: int) -> Any:
    return select(MemberRole.member_id).where(MemberRole.role_id == role_id)


class RoleService:
    """Stateless service for role administration."""

    # -----------------------------------------------------------------------
    # Reads
    # -----------------------------------------------------------------------

    async def list_roles(self, session: AsyncSession) -> list[Role]:
        """Return every role: custom roles by ``(position, id)``, then the builtin ones."""
        stmt = select(Role).order_by(Role.builtin != RoleBuiltin.CUSTOM, Role.position, Role.id)
        return list((await session.execute(stmt)).scalars().all())

    async def membership_counts(self, session: AsyncSession) -> dict[int, int]:
        """Return ``role_id -> number of project memberships holding it``.

        User and group memberships count alike, inherited ones included.
        Roles no membership holds are absent from the result.
        """
        stmt = select(MemberRole.role_id, func.count(func.distinct(MemberRole.member_id))).group_by(MemberRole.role_id)
        return {role_id: count for role_id, count in (await session.execute(stmt)).all()}

    async def get(self, session: AsyncSession, role_id: int) -> Role:
        """Return the role, or raise :class:`NotFoundError`."""
        role = (await session.execute(select(Role).where(Role.id == role_id))).scalar_one_or_none()
        if role is None:
            raise NotFoundError(f"Role {role_id} not found")
        return role

    async def usage(self, session: AsyncSession, role_id: int, limit: int = 50) -> RoleUsage:
        """Return how many memberships hold the role and the projects they are on, by key (at most *limit*)."""
        count = await session.scalar(
            select(func.count(func.distinct(MemberRole.member_id))).where(MemberRole.role_id == role_id)
        )
        rows = (
            await session.execute(
                select(Project.id, Project.key, Project.name)
                .where(Project.id.in_(select(Member.project_id).where(Member.id.in_(_usage_rows(role_id)))))
                .order_by(Project.key)
                .limit(limit)
            )
        ).all()
        return RoleUsage(
            membership_count=int(count or 0),
            projects=[{"id": pid, "key": key, "name": name} for pid, key, name in rows],
        )

    async def workflow_counts(self, session: AsyncSession, role_id: int) -> tuple[int, int]:
        """Return ``(workflow transitions, workflow field rules)`` defined for the role."""
        transitions = await session.scalar(
            select(func.count()).select_from(WorkflowTransition).where(WorkflowTransition.role_id == role_id)
        )
        rules = await session.scalar(
            select(func.count()).select_from(WorkflowFieldRule).where(WorkflowFieldRule.role_id == role_id)
        )
        return int(transitions or 0), int(rules or 0)

    # -----------------------------------------------------------------------
    # Create
    # -----------------------------------------------------------------------

    async def _require_name_free(self, session: AsyncSession, name: str, exclude_id: int | None = None) -> None:
        """Raise :class:`RoleNameTakenError` if *name* is taken (ignoring case) or is the anonymous role's name."""
        if name.lower() == ANONYMOUS_ROLE_NAME.lower():
            raise RoleNameTakenError(name)
        stmt = select(Role.id).where(func.lower(Role.name) == name.lower())
        if exclude_id is not None:
            stmt = stmt.where(Role.id != exclude_id)
        if (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None:
            raise RoleNameTakenError(name)

    async def create(self, session: AsyncSession, data: RoleCreate) -> tuple[Role, dict[str, Any]]:
        """Create a custom role, blank or as a copy of ``data.copy_from_role_id``.

        A copy takes the source's permissions, all four visibility settings,
        ``assignable`` (always True when copying a builtin role, which is
        never assignable) and its workflow transitions and field rules. It
        never takes ``builtin``, ``system_key`` or ``settings``. Returns the
        role and the facts for the ``role_created`` audit event.
        """
        await self._require_name_free(session, data.name)
        source = await self.get(session, data.copy_from_role_id) if data.copy_from_role_id is not None else None
        max_position = await session.scalar(select(func.max(Role.position)).where(Role.builtin == RoleBuiltin.CUSTOM))
        role = Role(name=data.name, position=(max_position or 0) + 1, builtin=RoleBuiltin.CUSTOM, settings={})
        if source is None:
            role.permissions = normalise_permissions(data.permissions or [])
            role.assignable = data.assignable
        else:
            role.permissions = list(source.permissions)
            role.assignable = True if source.builtin != RoleBuiltin.CUSTOM else source.assignable
            for name in _COPIED_VISIBILITY:
                setattr(role, name, getattr(source, name))
        session.add(role)
        await session.flush()

        transitions = rules = 0
        if source is not None:
            transitions, rules = await self._copy_workflow(session, source.id, role.id)
        await session.refresh(role)
        clear_role_cache(session)
        return role, {
            "role_name": role.name,
            "permissions": list(role.permissions),
            "copied_from": {"id": source.id, "name": source.name} if source is not None else None,
            "workflow_transitions_copied": transitions,
            "field_rules_copied": rules,
        }

    @staticmethod
    async def _copy_workflow(session: AsyncSession, source_id: int, target_id: int) -> tuple[int, int]:
        """Copy every workflow transition and field rule of *source_id* to *target_id*; return the counts."""
        t = WorkflowTransition
        transitions = await session.execute(
            insert(t).from_select(
                ["tracker_id", "role_id", "old_status_id", "new_status_id"],
                select(t.tracker_id, literal(target_id), t.old_status_id, t.new_status_id).where(
                    t.role_id == source_id
                ),
            )
        )
        r = WorkflowFieldRule
        rules = await session.execute(
            insert(r).from_select(
                ["tracker_id", "role_id", "status_id", "field_name", "rule"],
                select(r.tracker_id, literal(target_id), r.status_id, r.field_name, r.rule).where(
                    r.role_id == source_id
                ),
            )
        )
        return int(transitions.rowcount or 0), int(rules.rowcount or 0)  # type: ignore[attr-defined]

    # -----------------------------------------------------------------------
    # Update
    # -----------------------------------------------------------------------

    async def update(self, session: AsyncSession, role_id: int, data: RoleUpdate) -> tuple[Role, dict[str, Any]]:
        """Apply the fields present in *data* to the role; return it and what changed.

        ``data.lock_version`` must be the role's current version, else
        :class:`ConflictError`. The change dict holds only the keys that
        changed (``name``, ``assignable`` and the visibility settings as
        ``{old, new}``; ``permissions`` as ``{added, removed}``;
        ``full_access`` as ``{old, new}``), and is empty for a no-op, which
        writes nothing.
        """
        role = await self.get(session, role_id)
        if data.lock_version != role.lock_version:
            raise ConflictError("Someone else changed this role; reload to see their changes")
        fields = data.model_fields_set
        changes: dict[str, Any] = {}
        if "name" in fields and data.name is not None:
            await self._apply_name(session, role, data.name, changes)
        if "assignable" in fields and data.assignable is not None:
            self._apply_assignable(role, data.assignable, changes)
        if "permissions" in fields and data.permissions is not None:
            self._apply_permissions(role, data.permissions, changes)
        self._apply_visibility(role, data, changes)
        if not changes:
            return role, changes
        try:
            await session.flush()
        except StaleDataError as exc:
            raise ConflictError("Someone else changed this role; reload to see their changes") from exc
        await session.refresh(role)
        clear_role_cache(session)
        return role, changes

    async def _apply_name(self, session: AsyncSession, role: Role, name: str, changes: dict[str, Any]) -> None:
        if name == role.name:
            return
        if role.builtin != RoleBuiltin.CUSTOM:
            raise ValidationError("A built-in role cannot be renamed", field="name")
        await self._require_name_free(session, name, exclude_id=role.id)
        changes["name"] = {"old": role.name, "new": name}
        role.name = name

    @staticmethod
    def _apply_assignable(role: Role, assignable: bool, changes: dict[str, Any]) -> None:
        if assignable == role.assignable:
            return
        if role.builtin != RoleBuiltin.CUSTOM:
            raise ValidationError("A built-in role cannot be offered when adding members", field="assignable")
        changes["assignable"] = {"old": role.assignable, "new": assignable}
        role.assignable = assignable

    @staticmethod
    def _apply_permissions(role: Role, requested: list[str], changes: dict[str, Any]) -> None:
        new = normalise_permissions(requested)
        if role.builtin == RoleBuiltin.NON_MEMBER:
            refused = not_allowed_for_non_member(new)
            if refused:
                raise RolePermissionNotAllowedError(refused)
        old = [str(p) for p in role.permissions]
        if set(new) == set(old):
            return
        old_full, new_full = FULL_ACCESS in old, FULL_ACCESS in new
        added = [p for p in new if p not in old and p != FULL_ACCESS]
        removed = [p for p in old if p not in new and p != FULL_ACCESS]
        if added or removed:
            changes["permissions"] = {"added": added, "removed": removed}
        if old_full != new_full:
            changes["full_access"] = {"old": old_full, "new": new_full}
        role.permissions = new

    @staticmethod
    def _apply_visibility(role: Role, data: RoleUpdate, changes: dict[str, Any]) -> None:
        for name in _VISIBILITY_FIELDS:
            value = getattr(data, name)
            if name not in data.model_fields_set or value is None or value == getattr(role, name):
                continue
            changes[name] = {"old": getattr(role, name), "new": value}
            setattr(role, name, value)

    # -----------------------------------------------------------------------
    # Reorder
    # -----------------------------------------------------------------------

    async def move(self, session: AsyncSession, role_id: int, direction: Literal["up", "down"]) -> list[Role]:
        """Move a custom role one place up or down; return the custom roles in their new order.

        The custom roles are first renumbered densely from 1 by
        ``(position, id)`` (imported roles all arrive at position 1), then the
        role swaps places with its neighbour. Moving past either end, or
        moving a builtin role, changes nothing. Positions are written with
        Core ``UPDATE`` statements, so ``lock_version`` is not bumped.
        """
        role = await self.get(session, role_id)
        rows = (
            await session.execute(
                select(Role.id, Role.position)
                .where(Role.builtin == RoleBuiltin.CUSTOM)
                .order_by(Role.position, Role.id)
            )
        ).all()
        order = [rid for rid, _ in rows]
        current = dict(rows)
        if role.builtin == RoleBuiltin.CUSTOM:
            index = order.index(role.id)
            neighbour = index - 1 if direction == "up" else index + 1
            if 0 <= neighbour < len(order):
                order[index], order[neighbour] = order[neighbour], order[index]
        for position, rid in enumerate(order, start=1):
            if current[rid] != position:
                await session.execute(
                    update(Role)
                    .where(Role.id == rid)
                    .values(position=position)
                    .execution_options(synchronize_session=False)
                )
        result = await session.execute(
            select(Role)
            .where(Role.builtin == RoleBuiltin.CUSTOM)
            .order_by(Role.position, Role.id)
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    # -----------------------------------------------------------------------
    # Delete
    # -----------------------------------------------------------------------

    async def delete(self, session: AsyncSession, role_id: int) -> dict[str, Any]:
        """Delete a role no membership holds; return the facts for the ``role_deleted`` audit event.

        Refused, in this order: a builtin role (409 ``role_protected``,
        ``reason=builtin``), a system role (``reason`` is its key), a role any
        project membership holds (409 ``role_in_use`` with the projects). The
        role's workflow rows go with it (foreign key cascade), and the cached
        workflow transitions are dropped.
        """
        role = await self.get(session, role_id)
        if role.builtin != RoleBuiltin.CUSTOM:
            raise RoleProtectedError("builtin", "A built-in role cannot be deleted")
        if role.system_key is not None:
            raise RoleProtectedError(role.system_key, "This role is used by merge automation and cannot be deleted")
        usage = await self.usage(session, role_id)
        if usage.membership_count:
            raise RoleInUseError(usage)
        transitions, rules = await self.workflow_counts(session, role_id)
        facts = {
            "role_name": role.name,
            "permissions": list(role.permissions),
            "workflow_transitions_removed": transitions,
            "field_rules_removed": rules,
        }
        await session.execute(sql_delete(Role).where(Role.id == role_id).execution_options(synchronize_session=False))
        session.expunge(role)
        await WorkflowService().invalidate_cache()
        clear_role_cache(session)
        return facts
