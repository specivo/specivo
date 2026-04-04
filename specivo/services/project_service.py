"""Project service — CRUD, hierarchy, membership, and module management."""

from __future__ import annotations

import logging

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, ConflictError, NotFoundError
from specivo.core.utils import utcnow
from specivo.models.issue import Issue
from specivo.models.lookups import IssueStatus
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project, ProjectKeyAlias
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.wiki import Wiki, WikiPage
from specivo.schemas.project import KNOWN_MODULES, ProjectCreate, ProjectUpdate

logger = logging.getLogger(__name__)

# Default modules enabled for every new project
_DEFAULT_MODULES = ("issue_tracking", "wiki", "time_tracking")


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
        upper_key = key.upper()
        stmt = select(Project).where(Project.key == upper_key)
        result = await session.execute(stmt)
        project = result.scalar_one_or_none()
        if project is not None:
            return project
        # Alias fallback — check retired keys
        alias_stmt = (
            select(Project)
            .join(ProjectKeyAlias, ProjectKeyAlias.project_id == Project.id)
            .where(ProjectKeyAlias.old_key == upper_key)
        )
        result = await session.execute(alias_stmt)
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

        project = Project(
            name=data.name,
            identifier=data.identifier,
            key=data.key.upper(),
            description=data.description,
            parent_id=parent.id if parent else None,
            path=path,
            is_public=data.is_public,
            color=data.color,
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
        - Private projects they are a member of.
        """
        if user.is_admin:
            count_stmt = select(func.count()).select_from(Project)
            stmt = select(Project).order_by(Project.name).offset(offset).limit(limit)
        else:
            # Subquery: project IDs the user is a member of
            member_projects = select(Member.project_id).where(Member.user_id == user.id).scalar_subquery()
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
        """Apply partial update to an existing project."""
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
            conflict = await session.execute(
                select(Project.id).where(Project.key == new_key, Project.id != project.id)
            )
            if conflict.scalar_one_or_none() is not None:
                raise ConflictError(message=f"Project key '{new_key}' is already in use")

            # Check conflict with aliases
            alias_conflict = await session.execute(
                select(ProjectKeyAlias).where(ProjectKeyAlias.old_key == new_key)
            )
            existing_alias = alias_conflict.scalar_one_or_none()
            if existing_alias is not None:
                if existing_alias.project_id == project.id:
                    # Reverting to a previous key — delete the alias
                    await session.delete(existing_alias)
                else:
                    raise ConflictError(message=f"Key '{new_key}' is a retired key of another project")

            # Store old key as alias
            session.add(ProjectKeyAlias(
                old_key=project.key,
                project_id=project.id,
                renamed_at=utcnow(),
                renamed_by_id=admin_user.id,
            ))

            # Bulk re-key all issues
            rekey_stmt = (
                update(Issue)
                .where(Issue.project_id == project.id)
                .values(project_key=new_key)
            )
            result = await session.execute(rekey_stmt)
            issues_rekeyed = result.rowcount

            project.key = new_key

        if new_identifier and new_identifier != project.identifier:
            # Check conflict
            id_conflict = await session.execute(
                select(Project.id).where(
                    Project.identifier == new_identifier, Project.id != project.id
                )
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

    async def add_member(
        self,
        session: AsyncSession,
        project: Project,
        user_id: int,
        role_ids: list[int],
    ) -> Member:
        """Add a user as a project member with the specified roles.

        If the user is already a member, adds the new roles to the existing
        member record (skipping duplicates).
        """
        # Verify user exists
        user_result = await session.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            raise NotFoundError(f"User {user_id} not found")

        # Verify all roles exist
        roles_result = await session.execute(select(Role).where(Role.id.in_(role_ids)))
        roles = roles_result.scalars().all()
        if len(roles) != len(role_ids):
            found_ids = {r.id for r in roles}
            missing = set(role_ids) - found_ids
            raise NotFoundError(f"Roles not found: {sorted(missing)}")

        # Upsert member record
        existing_result = await session.execute(
            select(Member).where(
                Member.user_id == user_id,
                Member.project_id == project.id,
            )
        )
        member = existing_result.scalar_one_or_none()
        if member is None:
            member = Member(user_id=user_id, project_id=project.id)
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
        user_id: int,
        role_ids: list[int],
    ) -> Member:
        """Replace all roles for a project member with the given role_ids."""
        # Verify member exists
        result = await session.execute(
            select(Member).where(
                Member.user_id == user_id,
                Member.project_id == project.id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            raise NotFoundError(f"User {user_id} is not a member of project '{project.key}'")

        # Verify all roles exist
        roles_result = await session.execute(select(Role).where(Role.id.in_(role_ids)))
        roles = roles_result.scalars().all()
        if len(roles) != len(role_ids):
            found_ids = {r.id for r in roles}
            missing = set(role_ids) - found_ids
            raise NotFoundError(f"Roles not found: {sorted(missing)}")

        # Delete existing roles and replace
        await session.execute(
            delete(MemberRole).where(MemberRole.member_id == member.id)
        )
        for role_id in role_ids:
            session.add(MemberRole(member_id=member.id, role_id=role_id))

        await session.flush()
        return member

    async def remove_member(
        self,
        session: AsyncSession,
        project: Project,
        user_id: int,
    ) -> None:
        """Remove a user from a project (deletes member + member_roles via CASCADE)."""
        result = await session.execute(
            select(Member).where(
                Member.user_id == user_id,
                Member.project_id == project.id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            raise NotFoundError(f"User {user_id} is not a member of project '{project.key}'")

        await session.delete(member)
        await session.flush()

    async def list_members(
        self,
        session: AsyncSession,
        project: Project,
    ) -> list[dict]:
        """Return project members with their roles.

        Returns a list of dicts: {user_id, login, display_name, roles: [name]}.
        """
        # Load members + their user info
        members_result = await session.execute(select(Member).where(Member.project_id == project.id))
        members = members_result.scalars().all()

        if not members:
            return []

        member_ids = [m.id for m in members]
        user_ids = [m.user_id for m in members]

        # Batch load users
        users_result = await session.execute(select(User).where(User.id.in_(user_ids)))
        users_by_id = {u.id: u for u in users_result.scalars().all()}

        # Batch load member_roles + roles
        mr_result = await session.execute(select(MemberRole).where(MemberRole.member_id.in_(member_ids)))
        member_roles = mr_result.scalars().all()

        role_ids = list({mr.role_id for mr in member_roles})
        roles_result = await session.execute(select(Role).where(Role.id.in_(role_ids)))
        roles_by_id = {r.id: r for r in roles_result.scalars().all()}

        # Group roles by member_id
        roles_by_member: dict[int, list[str]] = {}
        for mr in member_roles:
            role = roles_by_id.get(mr.role_id)
            if role:
                roles_by_member.setdefault(mr.member_id, []).append(role.name)

        out = []
        for member in members:
            user = users_by_id.get(member.user_id)
            if user is None:
                continue
            out.append(
                {
                    "user_id": user.id,
                    "login": user.login,
                    "display_name": user.display_name,
                    "roles": roles_by_member.get(member.id, []),
                }
            )
        return out

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
        - member_count
        - wiki_page_count
        - modules (dict of module_name -> bool)
        - members (list of dicts with user_id, display_name, avatar_url)
        """
        stats: dict[int, dict] = {
            pid: {
                "open_count": 0,
                "closed_count": 0,
                "member_count": 0,
                "wiki_page_count": 0,
                "modules": {m: False for m in sorted(KNOWN_MODULES)},
                "members": [],
            }
            for pid in project_ids
        }

        if not project_ids:
            return stats

        # --- Issue counts (open vs closed) ---
        is_closed_sub = select(IssueStatus.id).where(IssueStatus.is_closed.is_(True)).scalar_subquery()
        issue_stmt = (
            select(
                Issue.project_id,
                func.count().filter(Issue.status_id.not_in(is_closed_sub)).label("open_count"),
                func.count().filter(Issue.status_id.in_(is_closed_sub)).label("closed_count"),
            )
            .where(Issue.project_id.in_(project_ids))
            .group_by(Issue.project_id)
        )
        issue_rows = (await session.execute(issue_stmt)).all()
        for row in issue_rows:
            stats[row.project_id]["open_count"] = row.open_count
            stats[row.project_id]["closed_count"] = row.closed_count

        # --- Member counts + member details (first 6 per project) ---
        member_stmt = (
            select(
                Member.project_id,
                User.id.label("user_id"),
                User.display_name,
                User.avatar_url,
            )
            .join(User, Member.user_id == User.id)
            .where(Member.project_id.in_(project_ids))
            .order_by(Member.project_id, Member.id)
        )
        member_rows = (await session.execute(member_stmt)).all()
        members_by_project: dict[int, list[dict]] = {}
        for row in member_rows:
            members_by_project.setdefault(row.project_id, []).append(
                {
                    "user_id": row.user_id,
                    "display_name": row.display_name,
                    "avatar_url": row.avatar_url,
                }
            )
        for pid, members in members_by_project.items():
            stats[pid]["member_count"] = len(members)
            stats[pid]["members"] = members[:6]  # first 6 for avatars

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
        module_stmt = (
            select(EnabledModule.project_id, EnabledModule.name)
            .where(EnabledModule.project_id.in_(project_ids))
        )
        module_rows = (await session.execute(module_stmt)).all()
        for row in module_rows:
            if row.project_id in stats:
                stats[row.project_id]["modules"][row.name] = True

        return stats
