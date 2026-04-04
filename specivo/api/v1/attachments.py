"""Attachments API — upload, download, delete file attachments."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.attachment import AttachmentOut, AttachmentUpdateSchema
from specivo.schemas.common import IdName
from specivo.services.attachment_service import AttachmentService

router = APIRouter(tags=["attachments"])
_service = AttachmentService()

_ALLOWED_CONTAINER_TYPES: frozenset[str] = frozenset({"Issue", "WikiPage", "Journal"})


async def _resolve_container_project_id(db: AsyncSession, container_type: str, container_id: int) -> int | None:
    """Return the project_id for the given container entity, or None if not found."""
    from sqlalchemy import select

    if container_type == "Issue":
        from specivo.models.issue import Issue

        result = await db.execute(select(Issue.project_id).where(Issue.id == container_id))
        return result.scalar_one_or_none()
    elif container_type == "WikiPage":
        from specivo.models.wiki import Wiki, WikiPage

        result = await db.execute(
            select(Wiki.project_id).join(WikiPage, WikiPage.wiki_id == Wiki.id).where(WikiPage.id == container_id)
        )
        return result.scalar_one_or_none()
    elif container_type == "Journal":
        from specivo.models.issue import Issue
        from specivo.models.journal import Journal

        result = await db.execute(
            select(Issue.project_id)
            .join(Journal, Journal.issue_id == Issue.id)
            .where(Journal.id == container_id, Journal.issue_id.isnot(None))
        )
        return result.scalar_one_or_none()
    return None


async def _check_container_access(db: AsyncSession, user: User, container_type: str, container_id: int) -> None:
    """Verify that the user can access the container entity. Raises 403 or 404."""
    if user.is_admin:
        return

    project_id = await _resolve_container_project_id(db, container_type, container_id)
    if project_id is None:
        raise NotFoundError(f"{container_type} {container_id} not found")

    from specivo.services.permission_service import check_permission

    if not await check_permission(user, project_id, "view_issues", db):
        # Check if user is at least a member or project is public
        from sqlalchemy import select

        from specivo.models.member import Member
        from specivo.models.project import Project

        member_result = await db.execute(
            select(Member.id).where(Member.user_id == user.id, Member.project_id == project_id)
        )
        if member_result.scalar_one_or_none() is not None:
            return  # Member — allow access

        project_result = await db.execute(select(Project.is_public).where(Project.id == project_id))
        is_public = project_result.scalar_one_or_none()
        if is_public:
            return  # Public project — allow access

        raise PermissionDeniedError("Access denied to this resource")


def _attachment_out(attachment, author_name: str | None = None) -> AttachmentOut:
    """Build AttachmentOut from an Attachment with eagerly-loaded author."""
    name = author_name or attachment.author.display_name
    return AttachmentOut(
        id=attachment.id,
        container_type=attachment.container_type,
        container_id=attachment.container_id,
        filename=attachment.filename,
        disk_filename=attachment.disk_filename,
        content_type=attachment.content_type,
        filesize=attachment.filesize,
        description=attachment.description,
        author=IdName(id=attachment.author_id, name=name),
        created_at=attachment.created_at,
        updated_at=attachment.updated_at,
    )


@router.post(
    "/attachments/",
    response_model=AttachmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_attachment(
    request: Request,
    file: UploadFile,
    container_type: str = Form(...),
    container_id: int = Form(...),
    description: str | None = Form(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AttachmentOut:
    """Upload a file attachment to a container entity.

    ``container_type``: ``"Issue"``, ``"WikiPage"``, or ``"Journal"``
    ``container_id``: the ID of the target entity
    """
    if container_type not in _ALLOWED_CONTAINER_TYPES:
        allowed = ", ".join(sorted(_ALLOWED_CONTAINER_TYPES))
        raise AppError(
            code="validation_error",
            message=f"Invalid container_type {container_type!r}. Must be one of: {allowed}",
            status_code=422,
        )

    await _check_container_access(db, current_user, container_type, container_id)

    try:
        attachment = await _service.upload(
            session=db,
            container_type=container_type,
            container_id=container_id,
            file=file,
            author=current_user,
            description=description,
            request=request,
        )
    except ValueError as exc:
        raise AppError(
            code="validation_error",
            message=str(exc),
            status_code=422,
        ) from exc

    return _attachment_out(attachment, author_name=current_user.display_name)


@router.get("/attachments/{attachment_id}/", response_model=AttachmentOut)
async def get_attachment(
    attachment_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AttachmentOut:
    """Get attachment metadata by ID."""
    attachment = await _service.get_by_id(db, attachment_id)
    await _check_container_access(db, current_user, attachment.container_type, attachment.container_id)
    return _attachment_out(attachment)


@router.get("/attachments/{attachment_id}/download/")
async def download_attachment(
    attachment_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """Download an attachment file as a streaming response."""
    attachment = await _service.get_by_id(db, attachment_id)
    await _check_container_access(db, current_user, attachment.container_type, attachment.container_id)

    file_path, content_type = await _service.download(attachment_id, db)

    # Always force download (Content-Disposition: attachment) to prevent
    # browsers from rendering potentially dangerous content inline (XSS).
    return FileResponse(
        path=str(file_path),
        media_type=content_type,
        filename=attachment.filename,
        headers={"Content-Disposition": f'attachment; filename="{attachment.filename}"'},
    )


@router.delete("/attachments/{attachment_id}/", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    request: Request,
    attachment_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an attachment (author or admin only)."""
    attachment = await _service.get_by_id(db, attachment_id)

    if not current_user.is_admin and attachment.author_id != current_user.id:
        raise PermissionDeniedError("You can only delete your own attachments (or be an admin)")

    await _service.delete(db, attachment, user=current_user, request=request)


@router.patch("/attachments/{attachment_id}/", response_model=AttachmentOut)
async def update_attachment(
    request: Request,
    attachment_id: int,
    payload: AttachmentUpdateSchema,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AttachmentOut:
    """Update an attachment's description and/or metadata (author or admin only).

    Re-indexes the attachment for search and logs the change to the audit trail.
    When ``metadata`` is provided, it is validated via ``AttachmentMetadata``
    Pydantic model and stored as JSONB.
    """
    attachment = await _service.get_by_id(db, attachment_id)
    await _check_container_access(db, current_user, attachment.container_type, attachment.container_id)

    if not current_user.is_admin and attachment.author_id != current_user.id:
        raise PermissionDeniedError("You can only update your own attachments (or be an admin)")

    # Capture author name before flush expires the eagerly-loaded relationship
    author_name = attachment.author.display_name

    # Update metadata if provided in payload
    if payload.metadata is not None:
        attachment.metadata = payload.metadata.model_dump(mode="json")

    # Update description only if explicitly provided
    if payload.description is not None:
        attachment = await _service.update_description(
            session=db,
            attachment=attachment,
            new_description=payload.description,
            user=current_user,
            request=request,
        )
    else:
        # If only metadata changed, still need to flush and re-index
        if payload.metadata is not None:
            await db.flush()
            try:
                from specivo.services.embedding_service import EmbeddingService

                await EmbeddingService().embed_attachment(db, attachment)
            except Exception:
                pass
            await db.refresh(attachment)

    return _attachment_out(attachment, author_name=author_name)
