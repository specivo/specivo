"""Attachments API — upload, download, delete file attachments."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.core.i18n import gettext as _
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.attachment import AttachmentOut, AttachmentUpdateSchema
from specivo.schemas.common import IdName
from specivo.services.attachment_service import AttachmentService

router = APIRouter(tags=["attachments"])
_service = AttachmentService()

_ALLOWED_CONTAINER_TYPES: frozenset[str] = frozenset({"Issue", "WikiPage", "Journal"})


async def _check_container_access(db: AsyncSession, user: User, container_type: str, container_id: int) -> None:
    """Verify that the user may open attachments of the container entity. Raises 404 otherwise.

    A hidden container answers exactly like a missing one, so private issues
    and wikis the user cannot read are not disclosed.
    """
    if not await _service.can_view_container(db, user, container_type, container_id):
        raise NotFoundError(f"{container_type} {container_id} not found")


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
        content_hash=attachment.content_hash,
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
    journal_id: int | None = Form(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AttachmentOut:
    """Upload a file attachment to a container entity.

    ``container_type``: ``"Issue"``, ``"WikiPage"``, or ``"Journal"``
    ``container_id``: the ID of the target entity

    The container must be one you may open (404 otherwise) and change
    (403 otherwise): ``edit_issues`` or ``add_issue_notes`` on an issue, or
    ``add_issues`` on one you created; ``manage_wiki`` on a wiki page;
    ``edit_notes``, or ``edit_own_notes`` on your own comment.

    A file attached to an issue is recorded in the issue's history. Pass
    ``journal_id`` to record it with your own edit or comment it came with
    (the comment's id), or with the earlier files of the same batch (the
    ``journal_id`` the first upload returned). Without it, or once another
    entry has been recorded since, the file gets an entry of its own.
    """
    if container_type not in _ALLOWED_CONTAINER_TYPES:
        allowed = ", ".join(sorted(_ALLOWED_CONTAINER_TYPES))
        raise AppError(
            code="validation_error",
            message=f"Invalid container_type {container_type!r}. Must be one of: {allowed}",
            status_code=422,
        )

    await _check_container_access(db, current_user, container_type, container_id)
    if not await _service.can_attach_to_container(db, current_user, container_type, container_id):
        raise PermissionDeniedError(_("You do not have permission to add files here."))

    try:
        uploaded = await _service.upload(
            session=db,
            container_type=container_type,
            container_id=container_id,
            file=file,
            author=current_user,
            description=description,
            request=request,
            journal_id=journal_id,
        )
    except ValueError as exc:
        raise AppError(
            code="validation_error",
            message=str(exc),
            status_code=422,
        ) from exc

    await db.commit()  # commit before response to avoid reload race condition
    out = _attachment_out(uploaded.attachment, author_name=current_user.display_name)
    out.journal_id = uploaded.journal.id if uploaded.journal is not None else None
    return out


@router.get("/attachments/{attachment_id}/", response_model=AttachmentOut)
async def get_attachment(
    attachment_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AttachmentOut:
    """Get attachment metadata by ID."""
    attachment = await _service.get_visible(db, attachment_id, current_user)
    return _attachment_out(attachment)


@router.get("/attachments/{attachment_id}/download/")
async def download_attachment(
    attachment_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """Download an attachment file as a streaming response."""
    attachment = await _service.get_visible(db, attachment_id, current_user)

    file_path, content_type = await _service.download(attachment_id, db)

    # Always force download (Content-Disposition: attachment) to prevent
    # browsers from rendering potentially dangerous content inline (XSS).
    # Let Starlette's FileResponse handle Content-Disposition encoding
    # (RFC 5987) — avoids header injection from filenames with quotes.
    return FileResponse(
        path=str(file_path),
        media_type=content_type,
        filename=attachment.filename,
        content_disposition_type="attachment",
    )


@router.delete("/attachments/{attachment_id}/", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    request: Request,
    attachment_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an attachment (author or admin only)."""
    attachment = await _service.get_visible(db, attachment_id, current_user)

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
    attachment = await _service.get_visible(db, attachment_id, current_user)

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
        # Only metadata changed: flushing re-indexes it (see search_index_service)
        if payload.metadata is not None:
            await db.flush()
            await db.refresh(attachment)

    return _attachment_out(attachment, author_name=author_name)
