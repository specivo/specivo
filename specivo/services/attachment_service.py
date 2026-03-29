"""AttachmentService — upload, download, delete, and list file attachments."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.core.config import get_settings
from specivo.core.exceptions import NotFoundError
from specivo.models.attachment import Attachment
from specivo.models.search import SearchSource
from specivo.models.user import User
from specivo.services.security_audit_service import SecurityAuditService

logger = logging.getLogger(__name__)

# Resolved lazily to avoid import-time get_settings() (breaks CI/test imports).
_upload_dir: Path | None = None
_max_file_size: int | None = None


def _get_upload_dir() -> Path:
    global _upload_dir
    if _upload_dir is None:
        _upload_dir = Path(get_settings().attachment_upload_dir)
    return _upload_dir


def _get_max_file_size() -> int:
    global _max_file_size
    if _max_file_size is None:
        _max_file_size = get_settings().attachment_max_size_mb * 1024 * 1024
    return _max_file_size


# Allowed MIME type allowlist (per Tracker_Attachments Section 5)
ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        # Images
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        # image/svg+xml removed: SVG can contain embedded JavaScript (XSS risk)
        # Documents
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/csv",
        # Archives
        "application/zip",
        "application/gzip",
        # Code / data
        "text/x-python",
        "text/x-ruby",
        "text/x-yaml",
        "text/yaml",
        "application/json",
        "text/xml",
        # text/html removed: inline HTML rendering enables XSS via uploaded files
        # Office
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        # Catch-all for tests / binary uploads
        "application/octet-stream",
    }
)


_audit = SecurityAuditService()


class AttachmentService:
    """Service layer for file attachment operations."""

    def _make_disk_filename(self, original_filename: str) -> str:
        """Generate a UUID-based disk filename, preserving the original extension."""
        ext = Path(original_filename).suffix.lower()
        return f"{uuid.uuid4()}{ext}"

    def _file_path(self, disk_filename: str) -> Path:
        """Return the absolute path for a given disk_filename."""
        return _get_upload_dir() / disk_filename

    async def upload(
        self,
        session: AsyncSession,
        container_type: str,
        container_id: int,
        file: UploadFile,
        author: User,
        description: str | None = None,
        request: Request | None = None,
    ) -> Attachment:
        """Save file to disk and create a DB record.

        Raises ``ValueError`` for invalid file size or content type.
        The file is deleted from disk if the DB insert fails (atomic cleanup).
        """
        # Read the file content
        content = await file.read()
        filesize = len(content)

        if filesize > _get_max_file_size():
            raise ValueError(f"File size {filesize} exceeds maximum {_get_max_file_size()} bytes (50 MB)")

        content_type = file.content_type or "application/octet-stream"
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise ValueError(
                f"Content type {content_type!r} is not allowed. "
                f"See attachment upload documentation for the allowed list."
            )

        original_filename = file.filename or "upload"
        disk_filename = self._make_disk_filename(original_filename)
        file_path = self._file_path(disk_filename)

        # Ensure the upload directory exists
        _get_upload_dir().mkdir(parents=True, exist_ok=True)

        # Write to disk
        file_path.write_bytes(content)

        try:
            attachment = Attachment(
                container_type=container_type,
                container_id=container_id,
                filename=original_filename,
                disk_filename=disk_filename,
                content_type=content_type,
                filesize=filesize,
                author_id=author.id,
                description=description,
            )
            session.add(attachment)
            await session.flush()
        except Exception:
            # Atomic cleanup: remove the file if DB insert fails
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        logger.info(
            "Uploaded attachment %d (%s, %d bytes) to %s/%d by user %d",
            attachment.id,
            original_filename,
            filesize,
            container_type,
            container_id,
            author.id,
        )

        # Index for search (inline, non-blocking on failure)
        try:
            from specivo.services.embedding_service import EmbeddingService

            await EmbeddingService().embed_attachment(session, attachment)
        except Exception:
            logger.debug("Embedding generation skipped for attachment %d", attachment.id)

        # Audit log
        try:
            project_id = await self._resolve_project_id(session, container_type, container_id)
            await _audit.log_event(
                session=session,
                event_type="attachment_uploaded",
                user_id=author.id,
                resource_type="Attachment",
                resource_id=attachment.id,
                project_id=project_id,
                details={
                    "filename": original_filename,
                    "content_type": content_type,
                    "filesize": filesize,
                    "container_type": container_type,
                    "container_id": container_id,
                    "has_description": description is not None,
                },
                request=request,
            )
        except Exception:
            logger.debug("Audit logging skipped for attachment %d upload", attachment.id)

        return attachment

    async def download(self, attachment_id: int, session: AsyncSession) -> tuple[Path, str]:
        """Return ``(file_path, content_type)`` for streaming response.

        Raises ``NotFoundError`` if the attachment or file does not exist.
        """
        result = await session.execute(select(Attachment).where(Attachment.id == attachment_id))
        attachment = result.scalar_one_or_none()
        if attachment is None:
            raise NotFoundError(f"Attachment {attachment_id} not found")

        file_path = self._file_path(attachment.disk_filename)
        if not file_path.exists():
            raise NotFoundError(f"Attachment {attachment_id} file not found on disk")

        content_type = attachment.content_type or "application/octet-stream"
        return file_path, content_type

    async def delete(
        self,
        session: AsyncSession,
        attachment: Attachment,
        user: User | None = None,
        request: Request | None = None,
    ) -> None:
        """Delete the DB record, search index, and the file on disk."""
        file_path = self._file_path(attachment.disk_filename)
        att_id = attachment.id
        att_filename = attachment.filename
        att_container_type = attachment.container_type
        att_container_id = attachment.container_id

        # Remove search index (before deleting the attachment row)
        try:
            result = await session.execute(
                select(SearchSource).where(
                    SearchSource.source_type == "attachment",
                    SearchSource.entity_id == att_id,
                )
            )
            source = result.scalar_one_or_none()
            if source is not None:
                await session.delete(source)
                await session.flush()
        except Exception:
            logger.debug("Search index cleanup skipped for attachment %d", att_id)

        await session.delete(attachment)
        await session.flush()

        # Remove from disk (best-effort — DB row is already gone)
        try:
            file_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not delete attachment file %s: %s", file_path, exc)

        logger.info("Deleted attachment id=%d filename=%r", att_id, att_filename)

        # Audit log
        try:
            project_id = await self._resolve_project_id(session, att_container_type, att_container_id)
            await _audit.log_event(
                session=session,
                event_type="attachment_deleted",
                user_id=user.id if user else None,
                resource_type="Attachment",
                resource_id=att_id,
                project_id=project_id,
                details={
                    "filename": att_filename,
                    "container_type": att_container_type,
                    "container_id": att_container_id,
                },
                request=request,
            )
        except Exception:
            logger.debug("Audit logging skipped for attachment %d delete", att_id)

    async def get_by_id(self, session: AsyncSession, attachment_id: int) -> Attachment:
        """Fetch an attachment by ID, eagerly loading author.

        Raises ``NotFoundError`` when not found.
        """
        result = await session.execute(
            select(Attachment).where(Attachment.id == attachment_id).options(selectinload(Attachment.author))
        )
        attachment = result.scalar_one_or_none()
        if attachment is None:
            raise NotFoundError(f"Attachment {attachment_id} not found")
        return attachment

    async def _resolve_project_id(
        self,
        session: AsyncSession,
        container_type: str,
        container_id: int,
    ) -> int | None:
        """Return the project_id for a container, or None."""
        from specivo.services.embedding_service import resolve_attachment_project_id

        return await resolve_attachment_project_id(session, container_type, container_id)

    async def update_description(
        self,
        session: AsyncSession,
        attachment: Attachment,
        new_description: str | None,
        user: User,
        request: Request | None = None,
    ) -> Attachment:
        """Update an attachment's description and re-index for search.

        Args:
            session: Async DB session.
            attachment: The attachment to update.
            new_description: New description text (or None to clear).
            user: The user performing the update.
            request: Optional request for audit batching.

        Returns:
            The updated attachment.
        """
        old_description = attachment.description
        attachment.description = new_description
        await session.flush()

        # Re-index for search (inline, non-blocking on failure)
        try:
            from specivo.services.embedding_service import EmbeddingService

            await EmbeddingService().embed_attachment(session, attachment)
        except Exception:
            logger.debug("Re-index skipped for attachment %d description update", attachment.id)

        # Audit log
        try:
            project_id = await self._resolve_project_id(session, attachment.container_type, attachment.container_id)
            await _audit.log_event(
                session=session,
                event_type="attachment_description_updated",
                user_id=user.id,
                resource_type="Attachment",
                resource_id=attachment.id,
                project_id=project_id,
                details={
                    "old_description": old_description,
                    "new_description": new_description,
                    "filename": attachment.filename,
                    "updated_by": user.login,
                },
                request=request,
            )
        except Exception:
            logger.debug("Audit logging skipped for attachment %d description update", attachment.id)

        # Refresh to reload attributes expired by flush (prevents MissingGreenlet
        # when the caller accesses updated_at or other columns in async context).
        await session.refresh(attachment)
        return attachment

    async def list_for_container(
        self,
        session: AsyncSession,
        container_type: str,
        container_id: int,
    ) -> list[Attachment]:
        """List attachments for a given container entity, ordered by created_at."""
        result = await session.execute(
            select(Attachment)
            .where(
                Attachment.container_type == container_type,
                Attachment.container_id == container_id,
            )
            .options(selectinload(Attachment.author))
            .order_by(Attachment.created_at.asc())
        )
        return list(result.scalars().all())
