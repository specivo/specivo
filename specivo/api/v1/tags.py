"""Tags API — per-project tag vocabulary and entity tagging.

Vocabulary management (create / rename / recolor / delete) requires
``manage_project``. Listing, autocomplete, and applying/removing tags on an
issue or wiki page require only project access — any member may tag, and new
tags are created on the fly. All mutations emit security audit events.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError, PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.project import Project
from specivo.models.tag import Tag
from specivo.models.user import User
from specivo.schemas.tag import (
    BulkTagRequest,
    EntityTagAdd,
    EntityTagsSet,
    TagCreate,
    TagOut,
    TagUpdate,
    TagWithUsageOut,
)
from specivo.services.issue_service import IssueService
from specivo.services.permission_service import check_permission
from specivo.services.project_service import ProjectService
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService
from specivo.services.tag_service import TagService
from specivo.services.wiki_service import WikiService

router = APIRouter(tags=["tags"])
_project_service = ProjectService()
_tag_service = TagService()
_issue_service = IssueService()
_wiki_service = WikiService()
_audit = SecurityAuditService()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_project(project_key: str, user: User, db: AsyncSession) -> Project:
    """Resolve a project by key and require access (404 if missing/inaccessible)."""
    project = await _project_service.get_by_key(db, project_key.upper())
    await _project_service.require_project_access(db, project, user)
    return project


async def _require_manage(project: Project, user: User, db: AsyncSession) -> None:
    """Raise 403 if *user* lacks manage_project on *project*."""
    if user.is_admin:
        return
    if not await check_permission(user, project.id, "manage_project", db):
        raise PermissionDeniedError("manage_project permission required")


def _tag_out(tag: Tag, project_key: str) -> TagOut:
    return TagOut(
        id=tag.id,
        name=tag.name,
        color=tag.color,
        project_key=project_key,
        created_at=tag.created_at,
    )


async def _log_tag(
    db: AsyncSession,
    request: Request,
    event: AuditEvent,
    user: User,
    project: Project,
    tag_id: int | None,
    details: dict,
) -> None:
    """Emit a tag audit event, swallowing audit failures (matches metadata schemas)."""
    try:
        await _audit.log_event(
            session=db,
            event_type=event,
            user_id=user.id,
            project_id=project.id,
            resource_type="tag",
            resource_id=tag_id,
            details={"project_key": project.key, **details},
            request=request,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Vocabulary: list / search / create / update / delete
# ---------------------------------------------------------------------------


@router.get("/projects/{project_key}/tags/", response_model=list[TagWithUsageOut])
async def list_tags(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TagWithUsageOut]:
    project = await _get_project(project_key, current_user, db)
    rows = await _tag_service.list_with_usage(db, project.id)
    return [
        TagWithUsageOut(
            id=tag.id,
            name=tag.name,
            color=tag.color,
            project_key=project.key,
            created_at=tag.created_at,
            issue_count=issue_count,
            wiki_count=wiki_count,
        )
        for tag, issue_count, wiki_count in rows
    ]


@router.get("/projects/{project_key}/tags/search/")
async def search_tags(
    project_key: str,
    q: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Autocomplete tags. Requires project access only."""
    project = await _get_project(project_key, current_user, db)
    tags = await _tag_service.search_for_project(db, project.id, q, limit=limit)
    return [{"id": t.id, "name": t.name, "color": t.color} for t in tags]


@router.get("/tags/search/")
async def search_tags_global(
    q: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Cross-project tag autocomplete, deduplicated by name.

    Suggests distinct tag names across every project the caller can access
    (member or public; admins see all). Used by the search-page tag filter.
    """
    return await _tag_service.search_across_projects(db, current_user, q, limit=limit)


@router.post(
    "/projects/{project_key}/tags/",
    response_model=TagOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_tag(
    project_key: str,
    data: TagCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TagOut:
    project = await _get_project(project_key, current_user, db)
    await _require_manage(project, current_user, db)
    tag = await _tag_service.create(db, project, data)
    await _log_tag(db, request, AuditEvent.TAG_CREATED, current_user, project, tag.id, {"name": tag.name})
    return _tag_out(tag, project.key)


async def _get_project_tag(project: Project, tag_id: int, db: AsyncSession) -> Tag:
    tag = await _tag_service.get_by_id(db, tag_id)
    if tag.project_id != project.id:
        raise NotFoundError(f"Tag {tag_id} not found in project '{project.key}'")
    return tag


@router.patch("/projects/{project_key}/tags/{tag_id}/", response_model=TagOut)
async def update_tag(
    project_key: str,
    tag_id: int,
    data: TagUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TagOut:
    project = await _get_project(project_key, current_user, db)
    await _require_manage(project, current_user, db)
    tag = await _get_project_tag(project, tag_id, db)
    tag = await _tag_service.update(db, tag, data)
    await _log_tag(db, request, AuditEvent.TAG_UPDATED, current_user, project, tag.id, {"name": tag.name})
    return _tag_out(tag, project.key)


@router.delete(
    "/projects/{project_key}/tags/{tag_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_tag(
    project_key: str,
    tag_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    project = await _get_project(project_key, current_user, db)
    await _require_manage(project, current_user, db)
    tag = await _get_project_tag(project, tag_id, db)
    name = tag.name
    await _tag_service.delete(db, tag)
    await _log_tag(db, request, AuditEvent.TAG_DELETED, current_user, project, tag_id, {"name": name})


# ---------------------------------------------------------------------------
# Issue tagging
# ---------------------------------------------------------------------------


async def _resolve_issue(issue_ref: str, user: User, db: AsyncSession):
    """Resolve an issue (visibility-checked) and its project."""
    issue = await _issue_service.get_by_display_key(db, issue_ref, user=user)
    project = await db.get(Project, issue.project_id)
    if project is None:  # pragma: no cover - defensive
        raise NotFoundError(f"Project for issue '{issue_ref}' not found")
    return issue, project


@router.get("/issues/{issue_ref}/tags/", response_model=list[TagOut])
async def list_issue_tags(
    issue_ref: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TagOut]:
    issue, project = await _resolve_issue(issue_ref, current_user, db)
    tags = await _tag_service.tags_for_issue(db, issue.id)
    return [_tag_out(t, project.key) for t in tags]


@router.put("/issues/{issue_ref}/tags/", response_model=list[TagOut])
async def set_issue_tags(
    issue_ref: str,
    data: EntityTagsSet,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TagOut]:
    issue, project = await _resolve_issue(issue_ref, current_user, db)
    diff = await _tag_service.set_issue_tags(db, project, issue.id, data.names, current_user)
    for name in diff["added"]:
        await _log_tag(
            db,
            request,
            AuditEvent.TAG_ADDED,
            current_user,
            project,
            None,
            {"name": name, "issue_ref": issue.display_key},
        )
    for name in diff["removed"]:
        await _log_tag(
            db,
            request,
            AuditEvent.TAG_REMOVED,
            current_user,
            project,
            None,
            {"name": name, "issue_ref": issue.display_key},
        )
    tags = await _tag_service.tags_for_issue(db, issue.id)
    return [_tag_out(t, project.key) for t in tags]


@router.post(
    "/issues/{issue_ref}/tags/",
    response_model=TagOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_issue_tag(
    issue_ref: str,
    data: EntityTagAdd,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TagOut:
    issue, project = await _resolve_issue(issue_ref, current_user, db)
    tag, created = await _tag_service.add_to_issue(db, project, issue.id, data.name, current_user)
    if created:
        await _log_tag(
            db,
            request,
            AuditEvent.TAG_ADDED,
            current_user,
            project,
            tag.id,
            {"name": tag.name, "issue_ref": issue.display_key},
        )
    return _tag_out(tag, project.key)


@router.delete(
    "/issues/{issue_ref}/tags/{tag_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_issue_tag(
    issue_ref: str,
    tag_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    issue, project = await _resolve_issue(issue_ref, current_user, db)
    removed = await _tag_service.remove_from_issue(db, issue.id, tag_id)
    if removed:
        await _log_tag(
            db,
            request,
            AuditEvent.TAG_REMOVED,
            current_user,
            project,
            tag_id,
            {"issue_ref": issue.display_key},
        )


# ---------------------------------------------------------------------------
# Wiki page tagging
# ---------------------------------------------------------------------------


@router.get("/projects/{project_key}/wiki/{slug}/tags/", response_model=list[TagOut])
async def list_wiki_tags(
    project_key: str,
    slug: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TagOut]:
    project = await _get_project(project_key, current_user, db)
    page, _ = await _wiki_service.get_page(db, project.id, slug)
    tags = await _tag_service.tags_for_wiki_page(db, page.id)
    return [_tag_out(t, project.key) for t in tags]


@router.put("/projects/{project_key}/wiki/{slug}/tags/", response_model=list[TagOut])
async def set_wiki_tags(
    project_key: str,
    slug: str,
    data: EntityTagsSet,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TagOut]:
    project = await _get_project(project_key, current_user, db)
    page, _ = await _wiki_service.get_page(db, project.id, slug)
    diff = await _tag_service.set_wiki_page_tags(db, project, page.id, data.names, current_user)
    for name in diff["added"]:
        await _log_tag(
            db,
            request,
            AuditEvent.TAG_ADDED,
            current_user,
            project,
            None,
            {"name": name, "wiki_slug": page.slug},
        )
    for name in diff["removed"]:
        await _log_tag(
            db,
            request,
            AuditEvent.TAG_REMOVED,
            current_user,
            project,
            None,
            {"name": name, "wiki_slug": page.slug},
        )
    tags = await _tag_service.tags_for_wiki_page(db, page.id)
    return [_tag_out(t, project.key) for t in tags]


@router.post(
    "/projects/{project_key}/wiki/{slug}/tags/",
    response_model=TagOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_wiki_tag(
    project_key: str,
    slug: str,
    data: EntityTagAdd,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TagOut:
    project = await _get_project(project_key, current_user, db)
    page, _ = await _wiki_service.get_page(db, project.id, slug)
    tag, created = await _tag_service.add_to_wiki_page(db, project, page.id, data.name, current_user)
    if created:
        await _log_tag(
            db,
            request,
            AuditEvent.TAG_ADDED,
            current_user,
            project,
            tag.id,
            {"name": tag.name, "wiki_slug": page.slug},
        )
    return _tag_out(tag, project.key)


@router.delete(
    "/projects/{project_key}/wiki/{slug}/tags/{tag_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_wiki_tag(
    project_key: str,
    slug: str,
    tag_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    project = await _get_project(project_key, current_user, db)
    page, _ = await _wiki_service.get_page(db, project.id, slug)
    removed = await _tag_service.remove_from_wiki_page(db, page.id, tag_id)
    if removed:
        await _log_tag(
            db,
            request,
            AuditEvent.TAG_REMOVED,
            current_user,
            project,
            tag_id,
            {"wiki_slug": page.slug},
        )


# ---------------------------------------------------------------------------
# Bulk issue tagging (rides on the bulk_operations feature)
# ---------------------------------------------------------------------------


@router.post("/projects/{project_key}/issues/bulk-tags/")
async def bulk_tag_issues(
    project_key: str,
    data: BulkTagRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Apply/remove tags across many issues. Requires the bulk_operations feature."""
    from specivo.core.features import has_feature

    if not has_feature("bulk_operations"):
        raise PermissionDeniedError("Bulk operations are not available")

    project = await _get_project(project_key, current_user, db)

    # Restrict to issues that belong to the project and are visible to the user.
    valid_ids: list[int] = []
    for issue_id in data.issue_ids:
        try:
            issue = await _issue_service.get_by_id(db, issue_id, user=current_user)
        except NotFoundError:
            continue
        if issue.project_id == project.id:
            valid_ids.append(issue.id)

    added = 0
    for name in data.add:
        added += await _tag_service.bulk_add_to_issues(db, project, valid_ids, name, current_user)
        await _log_tag(
            db,
            request,
            AuditEvent.TAG_ADDED,
            current_user,
            project,
            None,
            {"name": name, "issue_count": len(valid_ids), "bulk": True},
        )
    removed = 0
    for tag_id in data.remove:
        removed += await _tag_service.bulk_remove_from_issues(db, valid_ids, tag_id)
        await _log_tag(
            db,
            request,
            AuditEvent.TAG_REMOVED,
            current_user,
            project,
            tag_id,
            {"issue_count": len(valid_ids), "bulk": True},
        )

    return {"issues": len(valid_ids), "links_added": added, "links_removed": removed}
