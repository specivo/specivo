"""Project service — CRUD, hierarchy, membership, and module management."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import delete, func, or_, select, union
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from specivo.core.exceptions import AppError, ConflictError, NotFoundError, ValidationError
from specivo.core.utils import utcnow
from specivo.models.issue import Issue
from specivo.models.lookups import IssueStatus
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project, ProjectKeyAlias
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.models.wiki import Wiki, WikiPage
from specivo.schemas.project import KNOWN_MODULES, ProjectCreate, ProjectUpdate
from specivo.services.anonymous_user_service import refuse_anonymous_user
from specivo.services.computed_metadata_service import COMPUTED_METADATA_SETTINGS_KEY
from specivo.services.permission_service import member_principal_clause

logger = logging.getLogger(__name__)

# Default modules enabled for every new project
_DEFAULT_MODULES = ("issue_tracking", "wiki", "time_tracking")

PrincipalKind = Literal["user", "group"]


@dataclass(frozen=True, slots=True)
class Principal:
    """The holder of one ``members`` row: a user or a user group, never both.

    ``members`` allows exactly one of ``user_id`` / ``group_id`` to be set
    (``ck_members_one_principal``).  Passing the pair around as two optional
    arguments makes the invalid combinations — both set, neither set —
    representable at every call site, and each site has to be trusted to
    check.  This type makes them unrepresentable instead: the only ways in
    are :meth:`user`, :meth:`group`, :meth:`of` and :meth:`parse`, so
    "exactly one" is established once, at construction, and everything
    downstream can simply use it.

    The fields are deliberately not named ``user_id``/``group_id``: a
    ``Principal`` is a kind plus an id, and the properties of those names are
    provided for building queries and payloads.
    """

    kind: PrincipalKind
    id: int

    @classmethod
    def user(cls, user_id: int) -> Principal:
        """A principal holding a membership as a user."""
        return cls("user", user_id)

    @classmethod
    def group(cls, group_id: int) -> Principal:
        """A principal holding a membership as a user group."""
        return cls("group", group_id)

    @classmethod
    def of(cls, *, user_id: int | None = None, group_id: int | None = None) -> Principal:
        """Build a principal from an optional user id and an optional group id.

        Exactly one must be given.  This is the boundary where request
        payloads that name both, or neither, are rejected with a readable
        :class:`ValidationError` rather than reaching the database and
        failing the CHECK constraint.
        """
        if (user_id is None) == (group_id is None):
            raise ValidationError("Exactly one of user_id or group_id must be given", field="user_id")
        return cls.user(user_id) if user_id is not None else cls.group(group_id)  # type: ignore[arg-type]

    @classmethod
    def parse(cls, kind: str, principal_id: int) -> Principal:
        """Build a principal from a URL path segment naming its kind."""
        if kind not in ("user", "group"):
            raise ValidationError(
                f"principal_type must be 'user' or 'group', got '{kind}'",
                field="principal_type",
            )
        return cls(kind, principal_id)  # type: ignore[arg-type]

    @property
    def is_user(self) -> bool:
        return self.kind == "user"

    @property
    def user_id(self) -> int | None:
        """The user id, or ``None`` for a group principal."""
        return self.id if self.kind == "user" else None

    @property
    def group_id(self) -> int | None:
        """The group id, or ``None`` for a user principal."""
        return self.id if self.kind == "group" else None

    @property
    def label(self) -> str:
        """How to name this principal in an error message."""
        return f"User {self.id}" if self.kind == "user" else f"Group {self.id}"

    def member_row_clause(self) -> ColumnElement[bool]:
        """Match the one ``members`` row this principal holds, if any.

        Deliberately narrow, and deliberately not
        ``permission_service.member_principal_clause``: that one answers "does
        this row grant the user access", folding in the groups they belong to.
        This one addresses a row *to edit it*, so a user must never match a
        group's row and a group must never match a user's — otherwise removing
        a user could delete a group's grant, or the reverse.
        """
        if self.kind == "user":
            return Member.user_id == self.id
        return Member.group_id == self.id


class ProjectService:
    """Service layer for project operations."""

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _build_path(self, identifier: str, parent_path: str | None) -> str:
        """Build an ltree path.

        ltree labels must be alphanumeric (underscores allowed).
        Hyphens in identifiers are converted to underscores.
        """
        label = identifier.replace("-", "_")
        if parent_path:
            return f"{parent_path}.{label}"
        return label

    async def _get_by_key(self, session: AsyncSession, key: str) -> Project | None:
        """Look up a project by key, falling back to retired key aliases.

        Uses a single query with LEFT JOIN to avoid two round-trips.
        """
        upper_key = key.upper()
        stmt = (
            select(Project)
            .outerjoin(ProjectKeyAlias, ProjectKeyAlias.project_id == Project.id)
            .where(or_(Project.key == upper_key, ProjectKeyAlias.old_key == upper_key))
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    # -----------------------------------------------------------------------
    # Core CRUD
    # -----------------------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        data: ProjectCreate,
        creator_user: User,
    ) -> Project:
        """Create a root or child project.

        - Resolves ``parent_key`` to a ``parent_id`` and builds the ltree path.
        - Adds the creator as a Manager member (role builtin=0, name=Manager).
        - Enables default modules.
        """
        parent: Project | None = None
        if data.parent_key:
            parent = await self._get_by_key(session, data.parent_key)
            if parent is None:
                raise NotFoundError(f"Parent project '{data.parent_key}' not found")

            # Enforce maximum nesting depth
            from specivo.core.constants import MAX_PROJECT_DEPTH

            parent_depth = parent.path.count(".") + 1 if parent.path else 1
            if parent_depth >= MAX_PROJECT_DEPTH:
                raise AppError(
                    code="max_depth_exceeded",
                    message=f"Maximum project nesting depth of {MAX_PROJECT_DEPTH} exceeded.",
                    status_code=422,
                    details={"max_depth": MAX_PROJECT_DEPTH, "current_depth": parent_depth},
                )

        path = self._build_path(
            data.identifier,
            parent.path if parent else None,
        )

        settings: dict = {}
        if data.computed_metadata is not None:
            settings[COMPUTED_METADATA_SETTINGS_KEY] = data.computed_metadata

        project = Project(
            name=data.name,
            identifier=data.identifier,
            key=data.key.upper(),
            description=data.description,
            parent_id=parent.id if parent else None,
            path=path,
            is_public=data.is_public,
            color=data.color,
            settings=settings,
        )
        session.add(project)

        try:
            await session.flush()
        except IntegrityError as exc:
            # Let get_db dependency handle the rollback.
            # Re-raise as AppError so the API returns a structured 409.
            msg = str(exc.orig).lower() if exc.orig else ""
            if "identifier" in msg or "uq_projects_identifier" in msg or "projects_identifier_key" in msg:
                raise AppError(
                    code="conflict",
                    message=f"Project identifier '{data.identifier}' is already in use",
                    status_code=409,
                ) from exc
            if "key" in msg or "projects_key_key" in msg:
                raise AppError(
                    code="conflict",
                    message=f"Project key '{data.key}' is already in use",
                    status_code=409,
                ) from exc
            raise

        # Enable modules — use explicit list from request, or defaults
        if data.modules is not None:
            # Always enable issue_tracking; add requested modules
            modules_to_enable = {"issue_tracking"}
            for m in data.modules:
                if m in KNOWN_MODULES:
                    modules_to_enable.add(m)
            for module_name in sorted(modules_to_enable):
                session.add(EnabledModule(project_id=project.id, name=module_name))
        else:
            for module_name in _DEFAULT_MODULES:
                session.add(EnabledModule(project_id=project.id, name=module_name))

        await session.flush()
        return project

    async def get_by_key(self, session: AsyncSession, key: str) -> Project:
        """Get project by key; raises NotFoundError if missing."""
        project = await self._get_by_key(session, key)
        if project is None:
            raise NotFoundError(f"Project '{key}' not found")
        return project

    async def require_project_access(self, session: AsyncSession, project: Project, user: User) -> None:
        """Raise NotFoundError if non-admin user cannot access this project.

        Public projects: accessible to all authenticated users.
        Private projects: accessible to members — whether the membership is
        held by the user directly or by a user group they belong to.
        Returns 404 (not 403) to prevent project key enumeration.
        """
        if user.is_admin:
            return
        if project.is_public:
            return
        stmt = (
            select(Member.id)
            .where(
                Member.project_id == project.id,
                member_principal_clause(user.id),
            )
            .limit(1)
        )
        result = await session.execute(stmt)
        if result.scalar_one_or_none() is None:
            raise NotFoundError(f"Project '{project.key}' not found")

    async def get_parent_key(self, session: AsyncSession, project: Project) -> str | None:
        """Resolve the parent project's key, or None for root projects."""
        if project.parent_id is None:
            return None
        stmt = select(Project.key).where(Project.id == project.parent_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_projects(
        self,
        session: AsyncSession,
        user: User,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[Project], int]:
        """List projects visible to the user.

        Admins see all projects.  Regular users see:
        - All public projects.
        - Private projects they are a member of, directly or through a
          user group that holds the membership.
        """
        if user.is_admin:
            count_stmt = select(func.count()).select_from(Project)
            stmt = select(Project).order_by(Project.name).offset(offset).limit(limit)
        else:
            # Subquery: project IDs the user is a member of, via either principal
            member_projects = select(Member.project_id).where(member_principal_clause(user.id)).scalar_subquery()
            base = Project.is_public.is_(True) | Project.id.in_(member_projects)
            count_stmt = select(func.count()).select_from(Project).where(base)
            stmt = select(Project).where(base).order_by(Project.name).offset(offset).limit(limit)

        total = (await session.execute(count_stmt)).scalar_one()
        projects = (await session.execute(stmt)).scalars().all()
        return list(projects), total

    async def list_all_admin(self, session: AsyncSession, user: User) -> list[Project]:
        """List all projects (including archived). Admin use only."""
        if not user.is_admin:
            raise AppError(code="forbidden", message="Admin access required", status_code=403)
        stmt = select(Project).order_by(Project.status, Project.name)
        return list((await session.execute(stmt)).scalars().all())

    async def update(
        self,
        session: AsyncSession,
        project: Project,
        data: ProjectUpdate,
    ) -> Project:
        """Apply partial update to an existing project.

        When ``parent_id`` is present in the request payload (detected via
        ``model_fields_set``), the project is reparented.  A value of ``None``
        moves the project to root; an integer value sets a new parent.
        """
        if data.name is not None:
            project.name = data.name
        if data.description is not None:
            project.description = data.description
        if data.is_public is not None:
            project.is_public = data.is_public
        if data.status is not None:
            project.status = data.status
        if data.color is not None:
            project.color = data.color
        if data.computed_metadata is not None:
            # Reassign a new dict so SQLAlchemy marks the JSONB column dirty.
            new_settings = dict(project.settings or {})
            new_settings[COMPUTED_METADATA_SETTINGS_KEY] = data.computed_metadata
            project.settings = new_settings

        if "parent_id" in data.model_fields_set:
            project = await self._reparent(session, project, data.parent_id)
            return project

        session.add(project)
        await session.flush()
        await session.refresh(project)
        return project

    async def _reparent(
        self,
        session: AsyncSession,
        project: Project,
        new_parent_id: int | None,
    ) -> Project:
        """Change the parent of *project*, updating ltree paths for it and all descendants.

        Validates:
        - new_parent_id is not the project itself (self-loop).
        - new_parent_id is not a descendant of the project (cycle).
        - new_parent_id references an existing project (or is None for root).
        """
        from sqlalchemy import text

        from specivo.core.exceptions import AppError, NotFoundError

        # Self-assignment check
        if new_parent_id is not None and new_parent_id == project.id:
            raise AppError(
                code="invalid_parent",
                message="A project cannot be its own parent",
                status_code=400,
            )

        new_parent: Project | None = None
        if new_parent_id is not None:
            result = await session.execute(select(Project).where(Project.id == new_parent_id))
            new_parent = result.scalar_one_or_none()
            if new_parent is None:
                raise NotFoundError(f"Parent project with id {new_parent_id} not found")

            # Cycle detection: new_parent must not be a descendant of project.
            # A project is a descendant if its path starts with project.path + "." or equals it.
            old_path = project.path
            if new_parent.path == old_path or new_parent.path.startswith(old_path + "."):
                raise AppError(
                    code="cycle_detected",
                    message="Cannot set a descendant as parent (would create a cycle)",
                    status_code=400,
                )

        old_path = project.path
        new_path = self._build_path(project.identifier, new_parent.path if new_parent else None)

        # Bulk-update descendants first (before changing project.path)
        if old_path != new_path:
            await session.execute(
                text(
                    "UPDATE projects "
                    "SET path = :new_prefix || substring(path FROM length(:old_prefix) + 1) "
                    "WHERE path::text LIKE :old_prefix_dot"
                ),
                {
                    "new_prefix": new_path,
                    "old_prefix": old_path,
                    "old_prefix_dot": f"{old_path}.%",
                },
            )

        project.parent_id = new_parent_id
        project.path = new_path

        session.add(project)
        await session.flush()
        await session.refresh(project)
        return project

    async def rename(
        self,
        session: AsyncSession,
        project: Project,
        new_key: str | None,
        new_identifier: str | None,
        admin_user: User,
    ) -> tuple[Project, int]:
        """Rename project key and/or identifier. Returns (project, issues_rekeyed).

        Admin-only. Re-keys all issues atomically. Stores old key as alias
        for redirect lookups.
        """
        from sqlalchemy import text, update

        issues_rekeyed = 0

        if new_key and new_key != project.key:
            # Check conflict with live projects
            conflict = await session.execute(select(Project.id).where(Project.key == new_key, Project.id != project.id))
            if conflict.scalar_one_or_none() is not None:
                raise ConflictError(message=f"Project key '{new_key}' is already in use")

            # Check conflict with aliases
            alias_conflict = await session.execute(select(ProjectKeyAlias).where(ProjectKeyAlias.old_key == new_key))
            existing_alias = alias_conflict.scalar_one_or_none()
            if existing_alias is not None:
                if existing_alias.project_id == project.id:
                    # Reverting to a previous key — delete the alias
                    await session.delete(existing_alias)
                else:
                    raise ConflictError(message=f"Key '{new_key}' is a retired key of another project")

            # Store old key as alias
            session.add(
                ProjectKeyAlias(
                    old_key=project.key,
                    project_id=project.id,
                    renamed_at=utcnow(),
                    renamed_by_id=admin_user.id,
                )
            )

            # Bulk re-key all issues
            rekey_stmt = update(Issue).where(Issue.project_id == project.id).values(project_key=new_key)
            result = await session.execute(rekey_stmt)
            issues_rekeyed = result.rowcount

            project.key = new_key

        if new_identifier and new_identifier != project.identifier:
            # Check conflict
            id_conflict = await session.execute(
                select(Project.id).where(Project.identifier == new_identifier, Project.id != project.id)
            )
            if id_conflict.scalar_one_or_none() is not None:
                raise ConflictError(message=f"Identifier '{new_identifier}' is already in use")

            # Recalculate ltree path for project and descendants
            old_path = project.path
            new_label = new_identifier.replace("-", "_")
            # Replace the last segment of the path
            parts = old_path.rsplit(".", 1)
            new_path = f"{parts[0]}.{new_label}" if len(parts) > 1 else new_label

            # Update descendants
            await session.execute(
                text(
                    "UPDATE projects SET path = :new_prefix || substring(path FROM length(:old_prefix) + 1) "
                    "WHERE path::text = :old_prefix OR path::text LIKE :old_prefix_dot"
                ),
                {
                    "new_prefix": new_path,
                    "old_prefix": old_path,
                    "old_prefix_dot": f"{old_path}.%",
                },
            )

            project.identifier = new_identifier
            project.path = new_path

        session.add(project)
        await session.flush()
        await session.refresh(project)
        return project, issues_rekeyed

    async def delete(self, session: AsyncSession, project: Project) -> None:
        """Delete a project and all its children (CASCADE handles DB rows)."""
        await session.delete(project)
        await session.flush()

    async def create_child(
        self,
        session: AsyncSession,
        parent: Project,
        data: ProjectCreate,
        creator: User,
    ) -> Project:
        """Convenience wrapper: creates a child project under *parent*."""
        # Override parent_key to ensure the correct parent is used
        data_with_parent = data.model_copy(update={"parent_key": parent.key})
        return await self.create(session, data_with_parent, creator)

    # -----------------------------------------------------------------------
    # Membership
    # -----------------------------------------------------------------------

    async def _require_principal_exists(self, session: AsyncSession, principal: Principal) -> None:
        """Raise :class:`NotFoundError` if the user or group does not exist."""
        if principal.is_user:
            user = (await session.execute(select(User).where(User.id == principal.id))).scalar_one_or_none()
            if user is None:
                raise NotFoundError(f"User {principal.id} not found")
            refuse_anonymous_user(user, "The anonymous user cannot be a project member.")
        else:
            found = (
                await session.execute(select(UserGroup.id).where(UserGroup.id == principal.id))
            ).scalar_one_or_none()
            if found is None:
                raise NotFoundError(f"User group {principal.id} not found")

    async def _require_roles_exist(self, session: AsyncSession, role_ids: list[int]) -> None:
        """Raise :class:`NotFoundError` naming any role id that does not exist."""
        result = await session.execute(select(Role.id).where(Role.id.in_(role_ids)))
        found_ids = set(result.scalars().all())
        missing = set(role_ids) - found_ids
        if missing:
            raise NotFoundError(f"Roles not found: {sorted(missing)}")

    async def _find_member_row(
        self,
        session: AsyncSession,
        project: Project,
        principal: Principal,
    ) -> Member | None:
        """Return the membership row *principal* holds on *project*, if any."""
        result = await session.execute(
            select(Member).where(
                principal.member_row_clause(),
                Member.project_id == project.id,
            )
        )
        return result.scalar_one_or_none()

    async def add_member(
        self,
        session: AsyncSession,
        project: Project,
        principal: Principal,
        role_ids: list[int],
    ) -> Member:
        """Add a principal — a user or a user group — to *project* with roles.

        If the principal is already a member, the new roles are added to the
        existing membership row, skipping ones it already holds.
        """
        await self._require_principal_exists(session, principal)
        await self._require_roles_exist(session, role_ids)

        member = await self._find_member_row(session, project, principal)
        if member is None:
            member = Member(
                user_id=principal.user_id,
                group_id=principal.group_id,
                project_id=project.id,
            )
            session.add(member)
            await session.flush()

        # Fetch existing role assignments to avoid duplicates
        existing_mr_result = await session.execute(select(MemberRole.role_id).where(MemberRole.member_id == member.id))
        existing_role_ids = set(existing_mr_result.scalars().all())

        for role_id in role_ids:
            if role_id not in existing_role_ids:
                session.add(MemberRole(member_id=member.id, role_id=role_id))

        await session.flush()
        return member

    async def update_member_roles(
        self,
        session: AsyncSession,
        project: Project,
        principal: Principal,
        role_ids: list[int],
    ) -> Member:
        """Replace all roles held by *principal* on *project* with *role_ids*."""
        member = await self._find_member_row(session, project, principal)
        if member is None:
            raise NotFoundError(f"{principal.label} is not a member of project '{project.key}'")

        await self._require_roles_exist(session, role_ids)

        # Delete existing roles and replace
        await session.execute(delete(MemberRole).where(MemberRole.member_id == member.id))
        for role_id in role_ids:
            session.add(MemberRole(member_id=member.id, role_id=role_id))

        await session.flush()
        return member

    async def remove_member(
        self,
        session: AsyncSession,
        project: Project,
        principal: Principal,
    ) -> None:
        """Remove *principal* from *project* (member_roles go via CASCADE)."""
        member = await self._find_member_row(session, project, principal)
        if member is None:
            raise NotFoundError(f"{principal.label} is not a member of project '{project.key}'")

        await session.delete(member)
        await session.flush()

    async def count_membership_rows(self, session: AsyncSession, project: Project) -> int:
        """Return the number of membership rows on a project — its access grants.

        One row is one grant of a role set to one principal, so a group counts
        as one however many users it holds.  This is the number the project
        settings members tab reports, because that tab lists the rows
        themselves and each row is separately editable and removable.

        For a count of the *humans* those rows reach, which is what every
        people-shaped surface wants, use :meth:`count_people_with_access`.
        The two numbers differ as soon as a group is used, and they are
        deliberately named apart so no screen can show one while meaning the
        other.
        """
        result = await session.execute(select(func.count()).select_from(Member).where(Member.project_id == project.id))
        return result.scalar_one()

    async def count_people_with_access(self, session: AsyncSession, project: Project) -> int:
        """Return the number of distinct users the project's memberships reach.

        A user is counted once whether they hold a membership directly, belong
        to a group that holds one, or both — the ``UNION`` deduplicates.  This
        is the number shown wherever the UI says "people": the project cards,
        the admin projects table and the project overview.  Counting rows
        there would report a project whose access is entirely group-held as
        having one or two members when it in fact reaches a whole team.

        The counterpart is :meth:`count_membership_rows`.
        """
        direct = select(Member.user_id.label("user_id")).where(
            Member.project_id == project.id,
            Member.user_id.is_not(None),
        )
        via_group = (
            select(UserGroupMember.user_id.label("user_id"))
            .join(Member, Member.group_id == UserGroupMember.group_id)
            .where(Member.project_id == project.id)
        )
        stmt = select(func.count()).select_from(union(direct, via_group).subquery())
        return (await session.execute(stmt)).scalar_one()

    async def list_members(
        self,
        session: AsyncSession,
        project: Project,
        limit: int | None = None,
    ) -> list[dict]:
        """Return the **user-held** membership rows of a project, with their roles.

        Returns a list of dicts sorted by last login (most recent first).
        Pass ``limit`` to cap the number of results (useful for overview cards).

        Group-held rows are skipped, and that is this method's job rather than
        a gap in it.  Nearly every caller is an assignee or user picker — the
        issue, sprint, time and recurring-task screens, and the MCP
        ``list_members`` tool — and they need people to assign work to, which
        a group is not.  So this stays the user-only membership list and can
        be relied on to be one.  Group-held rows have their own shape and are
        returned by :meth:`list_group_memberships`; the callers that want both
        kinds (the members API endpoint) ask for both and concatenate.

        Each row carries ``principal_type == "user"`` so a row remains
        self-describing once the two lists are mixed.
        """
        stmt = (
            select(Member)
            .where(Member.project_id == project.id)
            .options(
                selectinload(Member.user),
                selectinload(Member.member_roles).joinedload(MemberRole.role),
            )
        )
        members_result = await session.execute(stmt)
        members = list(members_result.scalars().all())

        if not members:
            return []

        out = []
        for member in members:
            user = member.user
            if user is None:
                # Group-held membership row — see the docstring.
                continue
            role_names = [mr.role.name for mr in member.member_roles if mr.role is not None]
            role_ids = [mr.role.id for mr in member.member_roles if mr.role is not None]
            out.append(
                {
                    "principal_type": "user",
                    "user_id": user.id,
                    "login": user.login,
                    "display_name": user.display_name,
                    "avatar_url": user.avatar_url,
                    "avatar_color": (user.preferences or {}).get("avatar_color", ""),
                    "_last_login_at": user.last_login_at,
                    "roles": role_names,
                    "role_ids": role_ids,
                }
            )
        # Sort by last active (most recent first), None values last
        out.sort(key=lambda m: (m["_last_login_at"] is not None, m["_last_login_at"]), reverse=True)
        # Remove sort key — it's a datetime that can't be JSON-serialized
        for m in out:
            del m["_last_login_at"]
        if limit is not None:
            out = out[:limit]
        return out

    async def list_group_memberships(
        self,
        session: AsyncSession,
        project: Project,
    ) -> list[dict]:
        """Return the **group-held** membership rows of a project, with their roles.

        The counterpart to :meth:`list_members`, which covers the user-held
        rows.  A group has no login, display name or last-login date, so its
        row is shaped around what a group does have: its id, its name, and how
        many users the grant reaches.  Rows are ordered by name, since there
        is no "last active" to sort them by, and carry
        ``principal_type == "group"`` so they stay identifiable when the two
        lists are concatenated.
        """
        user_count = (
            select(func.count())
            .select_from(UserGroupMember)
            .where(UserGroupMember.group_id == UserGroup.id)
            .correlate(UserGroup)
            .scalar_subquery()
        )
        stmt = (
            select(Member, UserGroup, user_count)
            .join(UserGroup, UserGroup.id == Member.group_id)
            .where(Member.project_id == project.id)
            .options(selectinload(Member.member_roles).joinedload(MemberRole.role))
            .order_by(func.lower(UserGroup.name))
        )
        rows = (await session.execute(stmt)).all()

        return [
            {
                "principal_type": "group",
                "group_id": group.id,
                "name": group.name,
                "user_count": users,
                "roles": [mr.role.name for mr in member.member_roles if mr.role is not None],
                "role_ids": [mr.role.id for mr in member.member_roles if mr.role is not None],
            }
            for member, group, users in rows
        ]

    # -----------------------------------------------------------------------
    # Modules
    # -----------------------------------------------------------------------

    async def get_modules(
        self,
        session: AsyncSession,
        project: Project,
    ) -> dict[str, bool]:
        """Return a dict of module_name → enabled for all known modules."""
        result = await session.execute(select(EnabledModule.name).where(EnabledModule.project_id == project.id))
        enabled_names = set(result.scalars().all())
        return {name: (name in enabled_names) for name in sorted(KNOWN_MODULES)}

    async def toggle_module(
        self,
        session: AsyncSession,
        project: Project,
        module_name: str,
        enabled: bool,
    ) -> None:
        """Enable or disable a single module for *project*."""
        if module_name not in KNOWN_MODULES:
            raise AppError(
                code="validation_error",
                message=f"Unknown module: {module_name}",
                status_code=422,
            )

        result = await session.execute(
            select(EnabledModule).where(
                EnabledModule.project_id == project.id,
                EnabledModule.name == module_name,
            )
        )
        existing = result.scalar_one_or_none()

        if enabled and existing is None:
            session.add(EnabledModule(project_id=project.id, name=module_name))
            # Auto-create Wiki record when wiki module is enabled
            if module_name == "wiki":
                from specivo.services.wiki_service import WikiService

                wiki_svc = WikiService()
                await wiki_svc.get_or_create_wiki(session, project.id)
        elif not enabled and existing is not None:
            await session.delete(existing)

        await session.flush()

    async def set_modules(
        self,
        session: AsyncSession,
        project: Project,
        modules: dict[str, bool],
    ) -> dict[str, bool]:
        """Batch enable/disable modules; returns the resulting state."""
        for module_name, enabled in modules.items():
            await self.toggle_module(session, project, module_name, enabled)
        return await self.get_modules(session, project)

    # -----------------------------------------------------------------------
    # Stats
    # -----------------------------------------------------------------------

    async def load_project_stats(
        self,
        session: AsyncSession,
        project_ids: list[int],
    ) -> dict:
        """Batch-load stats for a set of project IDs.

        Returns a dict keyed by project_id with:
        - open_count, closed_count (issue stats)
        - member_count — distinct **people** with access, direct or via a group
        - group_count — how many of the memberships are held by a group
        - wiki_page_count
        - modules (dict of module_name -> bool)
        - members (list of dicts with user_id, display_name, avatar_url)

        ``member_count`` and ``members`` describe humans, not membership rows,
        because this feeds the avatar strips on project cards and the admin
        projects table — surfaces that show faces, which a group does not
        have.  ``group_count`` is carried alongside so those screens can say
        how many of the people arrive through a group rather than pretending
        every grant is direct.  The row count is a different number with its
        own method, :meth:`count_membership_rows`.
        """
        stats: dict[int, dict] = {
            pid: {
                "open_count": 0,
                "closed_count": 0,
                "member_count": 0,
                "group_count": 0,
                "wiki_page_count": 0,
                "modules": {m: False for m in sorted(KNOWN_MODULES)},
                "members": [],
            }
            for pid in project_ids
        }

        if not project_ids:
            return stats

        # --- Issue counts (open vs done/closed) ---
        done_sub = select(IssueStatus.id).where(IssueStatus.category.in_(["done", "closed"])).scalar_subquery()
        issue_stmt = (
            select(
                Issue.project_id,
                func.count().label("total"),
                func.count().filter(Issue.status_id.in_(done_sub)).label("done_count"),
            )
            .where(Issue.project_id.in_(project_ids))
            .group_by(Issue.project_id)
        )
        issue_rows = (await session.execute(issue_stmt)).all()
        for row in issue_rows:
            done = row.done_count or 0
            total = row.total or 0
            stats[row.project_id]["open_count"] = total - done
            stats[row.project_id]["closed_count"] = done

        # --- People with access + their avatars (first 6 per project) ---
        # Two queries rather than one UNION: the direct rows keep their
        # existing ``Member.id`` ordering so the avatar strip does not
        # reshuffle for projects that use no groups, and the group-reached
        # users are appended after them.  Duplicates — someone who is both a
        # direct member and in a member group — collapse in the merge below,
        # so the count is of distinct people either way.
        direct_stmt = (
            select(
                Member.project_id,
                User.id.label("user_id"),
                User.display_name,
                User.avatar_url,
                User.preferences,
            )
            .join(User, Member.user_id == User.id)
            .where(Member.project_id.in_(project_ids))
            .order_by(Member.project_id, Member.id)
        )
        via_group_stmt = (
            select(
                Member.project_id,
                User.id.label("user_id"),
                User.display_name,
                User.avatar_url,
                User.preferences,
            )
            .join(UserGroupMember, UserGroupMember.group_id == Member.group_id)
            .join(User, User.id == UserGroupMember.user_id)
            .where(Member.project_id.in_(project_ids))
            .order_by(Member.project_id, User.id)
        )

        members_by_project: dict[int, list[dict]] = {}
        seen_by_project: dict[int, set[int]] = {}
        for stmt in (direct_stmt, via_group_stmt):
            for row in (await session.execute(stmt)).all():
                seen = seen_by_project.setdefault(row.project_id, set())
                if row.user_id in seen:
                    continue
                seen.add(row.user_id)
                prefs = row.preferences or {}
                members_by_project.setdefault(row.project_id, []).append(
                    {
                        "user_id": row.user_id,
                        "display_name": row.display_name,
                        "avatar_url": row.avatar_url,
                        "avatar_color": prefs.get("avatar_color", ""),
                    }
                )
        for pid, members in members_by_project.items():
            stats[pid]["member_count"] = len(members)
            stats[pid]["members"] = members[:6]  # first 6 for avatars

        # --- Group-held membership rows per project ---
        group_stmt = (
            select(Member.project_id, func.count().label("group_count"))
            .where(Member.project_id.in_(project_ids), Member.group_id.is_not(None))
            .group_by(Member.project_id)
        )
        for group_row in (await session.execute(group_stmt)).all():
            stats[group_row.project_id]["group_count"] = group_row.group_count

        # --- Wiki page counts ---
        wiki_stmt = (
            select(Wiki.project_id, func.count(WikiPage.id).label("page_count"))
            .join(WikiPage, Wiki.id == WikiPage.wiki_id)
            .where(Wiki.project_id.in_(project_ids))
            .group_by(Wiki.project_id)
        )
        wiki_rows = (await session.execute(wiki_stmt)).all()
        for row in wiki_rows:
            stats[row.project_id]["wiki_page_count"] = row.page_count

        # --- Enabled modules ---
        module_stmt = select(EnabledModule.project_id, EnabledModule.name).where(
            EnabledModule.project_id.in_(project_ids)
        )
        module_rows = (await session.execute(module_stmt)).all()
        for row in module_rows:
            if row.project_id in stats:
                stats[row.project_id]["modules"][row.name] = True

        return stats
