"""User group administration — create, rename, delete, and populate groups.

A ``UserGroup`` is a membership principal: adding it to a project grants the
project's roles to everyone in the group.  This service owns the group itself
and its user list; putting a group *on a project* is project membership and
lives with the rest of that in ``ProjectService``.

Two things are worth knowing before reading further:

- Group names are unique case-insensitively, enforced by the
  ``uq_user_groups_name_ci`` expression index.  Every write here checks the
  name first and raises :class:`ConflictError`, so callers get a readable
  message instead of an ``IntegrityError``; the index stays the backstop for
  the race between the check and the insert.
- Deleting a group cascades in the database: its ``user_group_members`` rows
  and its ``members`` rows go with it, which is the whole feature — one
  delete revokes the access everywhere.  Because that information is gone
  immediately afterwards, :meth:`delete` counts it first and returns it in a
  :class:`GroupDeletion`.

The relationships on ``UserGroup`` are ``lazy="raise"``; everything here
queries explicitly rather than walking them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import ConflictError, NotFoundError
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember

# Sentinel for "argument not supplied", so ``description=None`` can mean
# "clear the description" and still be distinguishable from "leave it alone".
# Callers that build the argument conditionally pass this explicitly.
UNSET: Any = object()


@dataclass(frozen=True)
class GroupDeletion:
    """What deleting a group took with it.

    ``project_memberships_removed`` is the number of projects the group was
    granting access to; ``users_removed`` is how many users lost that access.
    Both are counted before the delete, because the cascade erases the rows
    they are derived from.
    """

    group_id: int
    name: str
    users_removed: int
    project_memberships_removed: int


class UserGroupService:
    """Stateless service for user group administration."""

    # -----------------------------------------------------------------------
    # Lookup
    # -----------------------------------------------------------------------

    async def get(self, session: AsyncSession, group_id: int) -> UserGroup:
        """Return the group, or raise :class:`NotFoundError`."""
        result = await session.execute(select(UserGroup).where(UserGroup.id == group_id))
        group = result.scalar_one_or_none()
        if group is None:
            raise NotFoundError(f"User group {group_id} not found")
        return group

    async def _require_name_free(self, session: AsyncSession, name: str, exclude_id: int | None = None) -> None:
        """Raise :class:`ConflictError` if *name* is taken, ignoring case."""
        stmt = select(UserGroup.id).where(func.lower(UserGroup.name) == name.lower())
        if exclude_id is not None:
            stmt = stmt.where(UserGroup.id != exclude_id)
        if (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None:
            raise ConflictError(f"A user group named '{name}' already exists", field="name")

    # -----------------------------------------------------------------------
    # Create / update / delete
    # -----------------------------------------------------------------------

    async def create(self, session: AsyncSession, name: str, description: str | None = None) -> UserGroup:
        """Create a group. Raises :class:`ConflictError` on a name collision."""
        await self._require_name_free(session, name)
        group = UserGroup(name=name, description=description)
        session.add(group)
        await session.flush()
        await session.refresh(group)
        return group

    async def update(
        self,
        session: AsyncSession,
        group_id: int,
        *,
        name: str | None = None,
        description: str | None = UNSET,
    ) -> UserGroup:
        """Rename a group and/or change its description.

        Omitted arguments are left untouched.  Passing ``description=None``
        clears the description.
        """
        group = await self.get(session, group_id)

        if name is not None and name != group.name:
            await self._require_name_free(session, name, exclude_id=group.id)
            group.name = name

        if description is not UNSET:
            group.description = description

        await session.flush()
        await session.refresh(group)
        return group

    async def delete(self, session: AsyncSession, group_id: int) -> GroupDeletion:
        """Delete a group and report what went with it.

        The ``user_group_members`` and ``members`` rows are removed by the
        database's ``ON DELETE CASCADE``.  Both are counted before the delete
        so the caller can be told what the delete cost.
        """
        group = await self.get(session, group_id)
        users_removed = await self.count_users(session, group.id)
        projects_removed = await self.count_project_memberships(session, group.id)
        name = group.name

        await session.delete(group)
        await session.flush()

        return GroupDeletion(
            group_id=group_id,
            name=name,
            users_removed=users_removed,
            project_memberships_removed=projects_removed,
        )

    # -----------------------------------------------------------------------
    # Counts
    # -----------------------------------------------------------------------

    async def count_users(self, session: AsyncSession, group_id: int) -> int:
        """Return the number of users in the group."""
        stmt = select(func.count()).select_from(UserGroupMember).where(UserGroupMember.group_id == group_id)
        return (await session.execute(stmt)).scalar_one()

    async def count_project_memberships(self, session: AsyncSession, group_id: int) -> int:
        """Return the number of projects the group holds a membership on."""
        stmt = select(func.count()).select_from(Member).where(Member.group_id == group_id)
        return (await session.execute(stmt)).scalar_one()

    # -----------------------------------------------------------------------
    # Listing
    # -----------------------------------------------------------------------

    async def list_groups(
        self,
        session: AsyncSession,
        q: str | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> tuple[list[dict], int]:
        """Return ``(rows, total_count)`` of groups, each row with its counts.

        *q* is an optional case-insensitive substring match on the name.  The
        two counts are correlated scalar subqueries rather than joins, so a
        group with many users still produces exactly one row.
        """
        user_count = (
            select(func.count())
            .select_from(UserGroupMember)
            .where(UserGroupMember.group_id == UserGroup.id)
            .correlate(UserGroup)
            .scalar_subquery()
        )
        project_count = (
            select(func.count())
            .select_from(Member)
            .where(Member.group_id == UserGroup.id)
            .correlate(UserGroup)
            .scalar_subquery()
        )

        stmt = select(UserGroup, user_count, project_count)
        count_stmt = select(func.count()).select_from(UserGroup)
        if q:
            pattern = f"%{q}%"
            stmt = stmt.where(UserGroup.name.ilike(pattern))
            count_stmt = count_stmt.where(UserGroup.name.ilike(pattern))

        stmt = stmt.order_by(func.lower(UserGroup.name)).offset(offset).limit(limit)

        rows = (await session.execute(stmt)).all()
        total = (await session.execute(count_stmt)).scalar_one()

        return [
            {
                "id": group.id,
                "name": group.name,
                "description": group.description,
                "created_at": group.created_at,
                "updated_at": group.updated_at,
                "user_count": users,
                "project_count": projects,
            }
            for group, users, projects in rows
        ], total

    async def list_users(
        self,
        session: AsyncSession,
        group_id: int,
        offset: int = 0,
        limit: int = 25,
    ) -> tuple[list[dict], int]:
        """Return ``(rows, total_count)`` of the users in a group."""
        await self.get(session, group_id)

        stmt = (
            select(User)
            .join(UserGroupMember, UserGroupMember.user_id == User.id)
            .where(UserGroupMember.group_id == group_id)
            .order_by(User.login)
            .offset(offset)
            .limit(limit)
        )
        count_stmt = select(func.count()).select_from(UserGroupMember).where(UserGroupMember.group_id == group_id)

        users = list((await session.execute(stmt)).scalars().all())
        total = (await session.execute(count_stmt)).scalar_one()

        return [
            {
                "user_id": user.id,
                "login": user.login,
                "display_name": user.display_name,
                "avatar_url": user.avatar_url,
            }
            for user in users
        ], total

    async def list_user_groups(self, session: AsyncSession, user_id: int) -> list[UserGroup]:
        """Return every group *user_id* belongs to, ordered by name."""
        stmt = (
            select(UserGroup)
            .join(UserGroupMember, UserGroupMember.group_id == UserGroup.id)
            .where(UserGroupMember.user_id == user_id)
            .order_by(func.lower(UserGroup.name))
        )
        return list((await session.execute(stmt)).scalars().all())

    async def list_projects(self, session: AsyncSession, group_id: int) -> list[dict]:
        """Return the projects the group is a member of, with the roles granted.

        This is what an admin needs before deleting a group: the access that
        disappears with it.  Roles are collected per project, so a group
        holding two roles on one project yields one row with both names.
        """
        stmt = (
            select(Project.id, Project.key, Project.name, Role.name)
            .join(Member, Member.project_id == Project.id)
            .outerjoin(MemberRole, MemberRole.member_id == Member.id)
            .outerjoin(Role, Role.id == MemberRole.role_id)
            .where(Member.group_id == group_id)
            .order_by(Project.key, Role.name)
        )
        rows = (await session.execute(stmt)).all()

        out: dict[int, dict] = {}
        for project_id, key, name, role_name in rows:
            entry = out.setdefault(
                project_id,
                {"project_id": project_id, "key": key, "name": name, "roles": []},
            )
            if role_name is not None and role_name not in entry["roles"]:
                entry["roles"].append(role_name)
        return list(out.values())

    # -----------------------------------------------------------------------
    # Group membership
    # -----------------------------------------------------------------------

    async def add_user(self, session: AsyncSession, group_id: int, user_id: int) -> bool:
        """Put a user into a group.

        Returns ``True`` if the user was added, ``False`` if they were already
        in the group — adding twice is a no-op, not an error.  Raises
        :class:`NotFoundError` if either the group or the user is missing.
        """
        await self.get(session, group_id)

        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user is None:
            raise NotFoundError(f"User {user_id} not found")

        existing = (
            await session.execute(
                select(UserGroupMember.id).where(
                    UserGroupMember.group_id == group_id,
                    UserGroupMember.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return False

        session.add(UserGroupMember(group_id=group_id, user_id=user_id))
        await session.flush()
        return True

    async def remove_user(self, session: AsyncSession, group_id: int, user_id: int) -> None:
        """Take a user out of a group.

        Raises :class:`NotFoundError` if the group does not exist or the user
        is not in it.
        """
        group = await self.get(session, group_id)

        link_id = (
            await session.execute(
                select(UserGroupMember.id).where(
                    UserGroupMember.group_id == group_id,
                    UserGroupMember.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if link_id is None:
            raise NotFoundError(f"User {user_id} is not in group '{group.name}'")

        await session.execute(sql_delete(UserGroupMember).where(UserGroupMember.id == link_id))
        await session.flush()
