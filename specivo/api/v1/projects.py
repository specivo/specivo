"""Projects API — CRUD, membership, and module management."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.admin import require_admin_api
from specivo.core.ai_billing import project_payer_default
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.core.security import get_current_user, get_reader
from specivo.models.project import Project
from specivo.models.user import User
from specivo.models.user_group import UserGroup
from specivo.schemas.common import PaginatedResponse
from specivo.schemas.project import (
    AssigneeSuggestionOut,
    BranchNameConfig,
    MemberAdd,
    MemberOut,
    MemberUpdateRoles,
    ModulesOut,
    ModuleToggle,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
)
from specivo.services.api_key_scope import is_global_admin
from specivo.services.branch_name import BranchNameSettings
from specivo.services.computed_metadata_service import computed_values
from specivo.services.concept_extraction import confidence_threshold, extraction_enabled, run_limit
from specivo.services.currency_service import reporting_currency_for
from specivo.services.entity_link_nodes import graph_metadata_keys
from specivo.services.link_suggestion_service import similarity_threshold
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import Principal, ProjectService
from specivo.services.security_audit_service import AuditEvent, MemberAction, SecurityAuditService
from specivo.services.similar_issues import project_setting_enabled

router = APIRouter(prefix="/projects", tags=["projects"])
_service = ProjectService()
_audit = SecurityAuditService()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _require_manage(
    project,
    user: User,
    db: AsyncSession,
    request: Request | None = None,
) -> None:
    """Raise 403 if user cannot manage the project. Logs failed attempts."""
    if user.is_admin:
        return
    allowed = await check_permission(user, project.id, Permission.MANAGE_PROJECT, db)
    if not allowed:
        try:
            await _audit.log_member_change(
                session=db,
                action=MemberAction.PERMISSION_DENIED,
                user_id=user.id,
                project_id=project.id,
                target_user_id=0,
                target_login="",
                request=request,
            )
            await db.commit()
        except Exception:  # noqa: BLE001, S110
            pass
        raise PermissionDeniedError("You do not have permission to manage this project")


async def _reachable(key: str, user: User, db: AsyncSession) -> Project:
    """Resolve project *key* if *user* may reach it, else raise 404.

    A missing key and a project *user* cannot reach get the same answer,
    naming the key as given (ADR-0004), so the permission checks that follow
    (403) only ever run for a project the caller already knows exists.
    """
    try:
        return await _service.get_readable_by_key(db, key.upper(), user)
    except NotFoundError:
        raise NotFoundError(f"Project '{key.upper()}' not found") from None


async def _can_manage(project, user: User, db: AsyncSession) -> bool:
    """Non-raising variant of :func:`_require_manage`, for response shaping."""
    if user.is_admin:
        return True
    return await check_permission(user, project.id, Permission.MANAGE_PROJECT, db)


async def _disclosed_computed_metadata(project, user: User, db: AsyncSession) -> dict | None:
    """Return the project's computed metadata map, or ``None`` if undisclosed.

    Managers get the real map (``{}`` when unconfigured) so that a configured
    project is distinguishable from an unconfigured one; everyone else gets
    ``None``. See ``ProjectOut.computed_metadata`` for the three states.
    """
    if not await _can_manage(project, user, db):
        return None
    return computed_values(project.settings)


def _branch_name(project) -> BranchNameConfig:
    """The project's branch name settings in effect, defaults filled in."""
    settings = BranchNameSettings.from_project_settings(project.settings)
    return BranchNameConfig(format=settings.format, types=dict(settings.types), default_type=settings.default_type)


async def _ancestors_reachable(db: AsyncSession, project: Project, user: User) -> bool:
    """Whether *user* may reach every ancestor of *project*.

    ``parent_id``, ``parent_key`` and ``path`` spell out the ancestors, and a
    public project can hang under a private parent: they are shown only when
    the whole lineage is reachable (anonymous visitors never see them).
    """
    parent_id = project.parent_id
    while parent_id is not None:
        parent = await db.get(Project, parent_id)
        if parent is None:
            return False
        try:
            await _service.require_project_access(db, parent, user)
        except NotFoundError:
            return False
        parent_id = parent.parent_id
    return True


def _project_out(
    project,
    parent_key: str | None,
    computed_metadata: dict | None = None,
    *,
    detail: bool = False,
    reporting_currency: str | None = None,
    lineage_visible: bool = True,
) -> ProjectOut:
    return ProjectOut(
        computed_metadata=computed_metadata,
        branch_name=_branch_name(project) if detail else None,
        similar_issues=project_setting_enabled(project.settings) if detail else None,
        graph_metadata_keys=list(graph_metadata_keys(project.settings)) if detail else None,
        similar_links_threshold=similarity_threshold(project.settings) if detail else None,
        concept_extraction=extraction_enabled(project.settings) if detail else None,
        concept_confidence_threshold=confidence_threshold(project.settings) if detail else None,
        concept_extraction_run_limit=run_limit(project.settings) if detail else None,
        reporting_currency_code=project.reporting_currency_code,
        reporting_currency=reporting_currency,
        ai_usage_payer_default=project_payer_default(project.settings) if detail else None,
        id=project.id,
        name=project.name,
        identifier=project.identifier,
        key=project.key,
        description=project.description,
        parent_id=project.parent_id if lineage_visible else None,
        parent_key=parent_key if lineage_visible else None,
        path=project.path if lineage_visible else None,
        is_public=project.is_public,
        inherit_members=project.inherit_members,
        status=project.status,
        issue_sequence=project.issue_sequence,
        color=project.color,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------


def _anonymous_project_out(project) -> ProjectOut:
    """Render a project for a visitor without an account.

    Only the project itself, never its place in the tree: ``path`` and
    ``parent_key`` spell out ancestor identifiers, and a public project can
    hang under a private parent. ``issue_sequence`` and ``inherit_members``
    are withheld for the reasons given on :class:`ProjectOut`. Each is left
    ``None`` — undisclosed — rather than filled with a stand-in.
    """
    return ProjectOut(
        id=project.id,
        name=project.name,
        identifier=project.identifier,
        key=project.key,
        description=project.description,
        is_public=project.is_public,
        status=project.status,
        color=project.color,
        created_at=project.created_at,
        updated_at=project.updated_at,
        parent_id=None,
        parent_key=None,
        path=None,
        inherit_members=None,
        issue_sequence=None,
        computed_metadata=None,
    )


@router.get("/", response_model=PaginatedResponse[ProjectOut])
async def list_projects(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_reader, scope="function"),
    db: AsyncSession = DB_SESSION,
) -> PaginatedResponse[ProjectOut]:
    """List the projects the caller may reach.

    ``ProjectService.accessible_projects_clause`` decides the set, so an
    anonymous visitor sees exactly the public, active, opted-in projects and
    nothing else — the listing cannot disagree with what the detail routes
    would serve.
    """
    projects, total = await _service.list_projects(db, current_user, offset=offset, limit=limit)

    if current_user.is_anonymous:
        return PaginatedResponse(
            total_count=total,
            offset=offset,
            limit=limit,
            items=[_anonymous_project_out(p) for p in projects],
        )

    items = []
    for p in projects:
        parent_key = await _service.get_parent_key(db, p)
        items.append(_project_out(p, parent_key, lineage_visible=await _ancestors_reachable(db, p, current_user)))

    return PaginatedResponse(total_count=total, offset=offset, limit=limit, items=items)


@router.post("/", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    data: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> ProjectOut:
    if not is_global_admin(current_user):
        raise PermissionDeniedError("Only admins can create projects")

    project = await _service.create(db, data)
    await db.commit()  # commit before response to avoid reload race condition
    parent_key = await _service.get_parent_key(db, project)
    # Creation is admin-only, so the creator may always see the map.
    return _project_out(
        project,
        parent_key,
        computed_values(project.settings),
        detail=True,
        reporting_currency=await reporting_currency_for(db, project),
    )


@router.get("/{key}/", response_model=ProjectOut)
async def get_project(
    key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> ProjectOut:
    project = await _reachable(key, current_user, db)

    parent_key = await _service.get_parent_key(db, project)
    computed = await _disclosed_computed_metadata(project, current_user, db)
    return _project_out(
        project,
        parent_key,
        computed,
        detail=True,
        reporting_currency=await reporting_currency_for(db, project),
        lineage_visible=await _ancestors_reachable(db, project, current_user),
    )


@router.patch("/{key}/", response_model=ProjectOut)
async def update_project(
    key: str,
    data: ProjectUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> ProjectOut:
    project = await _reachable(key, current_user, db)
    await _require_manage(project, current_user, db)

    project = await _service.update(db, project, data, actor=current_user, request=request)
    await db.commit()  # commit before response to avoid reload race condition
    parent_key = await _service.get_parent_key(db, project)
    # _require_manage passed, so the caller may always see the map.
    return _project_out(
        project,
        parent_key,
        computed_values(project.settings),
        detail=True,
        reporting_currency=await reporting_currency_for(db, project),
        lineage_visible=await _ancestors_reachable(db, project, current_user),
    )


@router.delete("/{key}/", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    key: str,
    request: Request,
    current_user: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = DB_SESSION,
) -> None:
    """Delete a project with all its issues, wiki pages and time entries. Instance admins only.

    Irreversible, so it is not a project manager's call (archiving, the
    reversible way, is admin-only too). A project with subprojects is refused
    with 409. The deletion is audited.
    """
    project = await _service.get_by_key(db, key.upper())
    details = {"project_key": project.key, "name": project.name, "identifier": project.identifier}
    project_id = project.id
    await _service.delete(db, project)
    await _audit.log_event(
        session=db,
        event_type=AuditEvent.PROJECT_DELETED,
        user_id=current_user.id,
        project_id=project_id,
        resource_type="project",
        resource_id=project_id,
        details=details,
        request=request,
    )


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


async def _member_row(
    db: AsyncSession,
    project,
    principal: Principal,
) -> MemberOut:
    """Re-read the membership row *principal* holds and render it.

    Written as a read-back rather than assembled from the service's return
    value so the response is exactly what a subsequent GET would show.
    """
    if principal.is_user:
        rows = await _service.list_members(db, project)
        key = "user_id"
    else:
        rows = await _service.list_group_memberships(db, project)
        key = "group_id"

    for row in rows:
        if row[key] == principal.id:
            return MemberOut(**row)

    raise AppError(code="internal_error", message="Member not found after write", status_code=500)


async def _log_member_change(
    db: AsyncSession,
    action: MemberAction,
    actor: User,
    project,
    principal: Principal,
    label: str,
    roles: list[str] | None = None,
    request: Request | None = None,
) -> None:
    """Write the audit row for a membership change, whatever holds it.

    *label* is the holder's human-readable name — a login for a user, a group
    name for a group.  Audit failures never fail the request they describe,
    which is why this swallows.
    """
    try:
        if principal.is_user:
            await _audit.log_member_change(
                session=db,
                action=action,
                user_id=actor.id,
                project_id=project.id,
                target_user_id=principal.id,
                target_login=label,
                roles=roles,
                request=request,
            )
        else:
            await _audit.log_member_change(
                session=db,
                action=action,
                user_id=actor.id,
                project_id=project.id,
                target_group_id=principal.id,
                target_group_name=label,
                roles=roles,
                request=request,
            )
    except Exception:  # noqa: BLE001, S110
        pass


async def _principal_label(db: AsyncSession, principal: Principal) -> str:
    """Return the login or group name for *principal*, or "" if it is gone."""
    if principal.is_user:
        stmt = select(User.login).where(User.id == principal.id)
    else:
        stmt = select(UserGroup.name).where(UserGroup.id == principal.id)
    try:
        return (await db.execute(stmt)).scalar_one_or_none() or ""
    except Exception:  # noqa: BLE001
        return ""


@router.get("/{key}/members/", response_model=list[MemberOut])
async def list_members(
    key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> list[MemberOut]:
    """List every membership on the project — users first, then user groups."""
    project = await _reachable(key, current_user, db)
    rows = await _service.list_members(db, project)
    rows += await _service.list_group_memberships(db, project)
    return [MemberOut(**row) for row in rows]


@router.get("/{key}/assignees/search/", response_model=list[AssigneeSuggestionOut])
async def search_assignees(
    key: str,
    q: str | None = Query(default=None, max_length=100, description="Substring of the login or display name."),
    offset: int = Query(default=0, ge=0, description="Matches to skip, to page through them."),
    limit: int = Query(default=20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> list[AssigneeSuggestionOut]:
    """Search the people issues of the project can be assigned to.

    The same people the issue form's assignee field offers: users holding a
    membership on the project themselves, never a system account. Ordered by
    display name and paged with ``offset``/``limit``; a page shorter than
    ``limit`` is the last. Needs an account and access to the project (404
    otherwise, for a private project).
    """
    project = await _reachable(key, current_user, db)
    users = await _service.search_assignable_users(db, project, q, offset=offset, limit=limit)
    return [
        AssigneeSuggestionOut(
            id=u.id,
            login=u.login,
            display_name=u.display_name,
            avatar_url=u.avatar_url,
            avatar_color=str((u.preferences or {}).get("avatar_color") or ""),
            is_service_account=bool(u.is_service_account),
        )
        for u in users
    ]


@router.post("/{key}/members/", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
async def add_member(
    key: str,
    data: MemberAdd,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> MemberOut:
    """Add a user or a user group to the project with the given roles."""
    project = await _reachable(key, current_user, db)
    await _require_manage(project, current_user, db, request)

    principal = Principal.of(user_id=data.user_id, group_id=data.group_id)
    await _service.add_member(db, project, principal, data.role_ids)

    result = await _member_row(db, project, principal)
    label = result.login if principal.is_user else result.name
    await _log_member_change(
        db,
        MemberAction.ADDED,
        current_user,
        project,
        principal,
        label or "",
        roles=result.roles,
        request=request,
    )
    return result


@router.delete("/{key}/members/{principal_type}/{principal_id}/", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    key: str,
    principal_type: str,
    principal_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> None:
    """Remove a membership. *principal_type* is ``user`` or ``group``."""
    principal = Principal.parse(principal_type, principal_id)
    project = await _reachable(key, current_user, db)
    await _require_manage(project, current_user, db, request)

    # Capture the holder's name before the delete takes it out of reach.
    label = await _principal_label(db, principal)

    await _service.remove_member(db, project, principal)

    await _log_member_change(db, MemberAction.REMOVED, current_user, project, principal, label, request=request)


@router.patch("/{key}/members/{principal_type}/{principal_id}/", response_model=MemberOut)
async def update_member_roles(
    key: str,
    principal_type: str,
    principal_id: int,
    data: MemberUpdateRoles,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> MemberOut:
    """Replace all roles held by one membership. ``user`` or ``group``."""
    principal = Principal.parse(principal_type, principal_id)
    project = await _reachable(key, current_user, db)
    await _require_manage(project, current_user, db, request)

    await _service.update_member_roles(db, project, principal, data.role_ids)

    result = await _member_row(db, project, principal)
    label = result.login if principal.is_user else result.name
    await _log_member_change(
        db,
        MemberAction.ROLES_CHANGED,
        current_user,
        project,
        principal,
        label or "",
        roles=result.roles,
        request=request,
    )
    return result


# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------


@router.get("/{key}/modules/", response_model=ModulesOut)
async def get_modules(
    key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> ModulesOut:
    project = await _reachable(key, current_user, db)
    modules = await _service.get_modules(db, project)
    return ModulesOut(modules=modules)


@router.patch("/{key}/modules/", response_model=ModulesOut)
async def update_modules(
    key: str,
    data: ModuleToggle,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> ModulesOut:
    project = await _reachable(key, current_user, db)
    await _require_manage(project, current_user, db)

    modules = await _service.set_modules(db, project, data.modules)
    return ModulesOut(modules=modules)
