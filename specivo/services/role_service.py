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
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role, RoleBuiltin
from specivo.models.workflow import WorkflowFieldRule, WorkflowTransition


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
