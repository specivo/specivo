"""AttachmentService — upload, download, delete, and list file attachments."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import Request, UploadFile
from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from specivo.core.config import get_settings
from specivo.core.exceptions import NotFoundError
from specivo.models.attachment import Attachment
from specivo.models.project import Project
from specivo.models.search import SearchSource
from specivo.models.user import User
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

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

    async def upload_from_path(
        self,
        session: AsyncSession,
        container_type: str,
        container_id: int,
        source_path: Path,
        original_filename: str,
        author: User,
        description: str | None = None,
        content_type: str | None = None,
        *,
        skip_search_index: bool = False,
        copy_file: bool = True,
    ) -> Attachment:
        """Attach a file that already exists on disk.

        For migrations, where the bytes come from another tracker's storage
        rather than an HTTP upload. Differs from :meth:`upload` in three ways,
        each because the file is historical rather than newly offered:

        * The content-type allowlist is not applied. It governs what this
          instance accepts today; a file attached years ago in a source system
          predates that policy, and dropping it would lose data the operator
          asked to migrate.
        * The size limit is not applied, for the same reason.
        * ``skip_search_index`` lets a bulk caller index everything once at the
          end rather than per file.
        * ``copy_file=False`` does everything except write the copy. A dry-run
          import rolls its transaction back, but a file already written would
          stay written, so the copy is the one step it has to leave out. The
          bytes are still read, which is where the failures are.

        Size and hash are computed from the bytes actually copied rather than
        trusted from the source's own record, so a file that was truncated or
        replaced underneath the source database is described correctly here.

        Raises ``FileNotFoundError`` when the file is not readable.
        """
        import hashlib
        import shutil

        if not source_path.is_file():
            raise FileNotFoundError(f"Attachment file not found: {source_path}")

        digest = hashlib.sha256()
        filesize = 0
        with source_path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                filesize += len(chunk)
        content_hash = digest.hexdigest()

        _get_upload_dir().mkdir(parents=True, exist_ok=True)

        filename = original_filename or source_path.name or "attachment"
        project_id = await self._resolve_project_id(session, container_type, container_id)
        if project_id is not None:
            filename = await self._unique_filename_in_project(session, project_id, filename, content_hash)

        disk_filename = self._make_disk_filename(filename)
        file_path = self._file_path(disk_filename)
        if copy_file:
            shutil.copyfile(source_path, file_path)

        try:
            attachment = Attachment(
                container_type=container_type,
                container_id=container_id,
                filename=filename,
                disk_filename=disk_filename,
                content_type=content_type or "application/octet-stream",
                filesize=filesize,
                author_id=author.id,
                description=description,
                content_hash=content_hash,
            )
            session.add(attachment)
            await session.flush()
        except Exception:
            # Atomic cleanup: the copy is only wanted if the row lands.
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        if not skip_search_index:
            try:
                from specivo.services.embedding_service import EmbeddingService

                await EmbeddingService().embed_attachment(session, attachment)
            except Exception:
                logger.debug("Embedding generation skipped for attachment %d", attachment.id)

        logger.debug(
            "Imported attachment %d (%s, %d bytes) onto %s/%d",
            attachment.id,
            filename,
            filesize,
            container_type,
            container_id,
        )
        return attachment

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

        # Ensure the upload directory exists
        _get_upload_dir().mkdir(parents=True, exist_ok=True)

        # Compute content hash
        import hashlib

        content_hash = hashlib.sha256(content).hexdigest()

        # Auto-rename if filename collides in the project (different content)
        project_id = await self._resolve_project_id(session, container_type, container_id)
        if project_id is not None:
            original_filename = await self._unique_filename_in_project(
                session,
                project_id,
                original_filename,
                content_hash,
            )

        disk_filename = self._make_disk_filename(original_filename)
        file_path = self._file_path(disk_filename)
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
                content_hash=content_hash,
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
                event_type=AuditEvent.ATTACHMENT_UPLOADED,
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
                event_type=AuditEvent.ATTACHMENT_DELETED,
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

    async def _unique_filename_in_project(
        self,
        session: AsyncSession,
        project_id: int,
        filename: str,
        content_hash: str,
    ) -> str:
        """Return a unique filename within the project.

        If a file with the same name and same hash exists, return the name as-is.
        If a file with the same name but different hash exists, append _1, _2, etc.
        """
        existing = await self._get_project_attachments_by_name(
            session,
            project_id,
            filename,
        )
        if not existing:
            return filename

        # Same content already uploaded with this name — keep it
        for att in existing:
            if att.content_hash == content_hash:
                return filename

        # Different content — find next available suffix
        stem, dot, ext = filename.rpartition(".")
        if not dot:
            stem, ext = filename, ""

        n = 1
        while True:
            candidate = f"{stem}_{n}.{ext}" if ext else f"{stem}_{n}"
            conflict = await self._get_project_attachments_by_name(
                session,
                project_id,
                candidate,
            )
            if not conflict:
                return candidate
            # Check if any conflict has same hash (reuse that name)
            for att in conflict:
                if att.content_hash == content_hash:
                    return candidate
            n += 1

    async def _get_project_attachments_by_name(
        self,
        session: AsyncSession,
        project_id: int,
        filename: str,
    ) -> list[Attachment]:
        """Find attachments with a given filename across the project."""
        from specivo.models.issue import Issue
        from specivo.models.wiki import Wiki, WikiPage

        # Issue attachments
        issue_att = await session.execute(
            select(Attachment)
            .join(Issue, (Attachment.container_type == "Issue") & (Attachment.container_id == Issue.id))
            .where(Issue.project_id == project_id, Attachment.filename == filename)
        )
        # WikiPage attachments
        wiki_att = await session.execute(
            select(Attachment)
            .join(WikiPage, (Attachment.container_type == "WikiPage") & (Attachment.container_id == WikiPage.id))
            .join(Wiki, WikiPage.wiki_id == Wiki.id)
            .where(Wiki.project_id == project_id, Attachment.filename == filename)
        )
        return list(issue_att.scalars().all()) + list(wiki_att.scalars().all())

    @staticmethod
    def download_url(attachment_id: int) -> str:
        """Return the download URL of an attachment."""
        return f"/api/v1/attachments/{attachment_id}/download/"

    async def visible_attachments_stmt(
        self,
        session: AsyncSession,
        project: Project,
        reader: User,
    ) -> Select[tuple[int, str, str, int]] | None:
        """Select the issue and wiki-page attachments of *project* that *reader* may open.

        Rows are ``(id, filename, container_type, container_id)``, oldest
        first, so a caller folding them into a filename map lets the newest
        visible file win. Callers may narrow the statement further (for
        example to a single container) before executing it.

        - Wiki-page attachments take part only when *reader* holds
          ``view_wiki`` in the project.
        - Issue attachments take part only for issues *reader* may see,
          judged by ``IssueService.project_visible_issues_clause`` — the
          predicate behind the project issue list.
        - Admins see everything; the anonymous user sees nothing, matching
          the API, which drops attachments from every anonymous response.

        Returns None when *reader* may open no attachment in the project.
        """
        from specivo.models.issue import Issue
        from specivo.models.wiki import Wiki, WikiPage
        from specivo.services.issue_service import IssueService
        from specivo.services.permission_service import Permission, check_permission

        if reader.is_anonymous:
            return None

        branches: list[ColumnElement[bool]] = []

        issue_visibility = await IssueService().project_visible_issues_clause(session, reader, project)
        issue_branch = and_(Attachment.container_type == "Issue", Issue.project_id == project.id)
        if issue_visibility is not None:
            issue_branch = and_(issue_branch, issue_visibility)
        branches.append(issue_branch)

        if await check_permission(reader, project.id, Permission.VIEW_WIKI, session):
            branches.append(and_(Attachment.container_type == "WikiPage", Wiki.project_id == project.id))

        return (
            select(Attachment.id, Attachment.filename, Attachment.container_type, Attachment.container_id)
            .outerjoin(Issue, and_(Attachment.container_type == "Issue", Attachment.container_id == Issue.id))
            .outerjoin(
                WikiPage,
                and_(Attachment.container_type == "WikiPage", Attachment.container_id == WikiPage.id),
            )
            .outerjoin(Wiki, WikiPage.wiki_id == Wiki.id)
            .where(or_(*branches))
            .order_by(Attachment.created_at.asc(), Attachment.id.asc())
        )

    async def build_project_attachment_map(
        self,
        session: AsyncSession,
        project: Project,
        reader: User,
    ) -> dict[str, str]:
        """Build a filename → download URL map of the attachments *reader* may open in *project*.

        Used by the wiki markdown renderer to resolve inline image references.
        Only attachments from ``visible_attachments_stmt`` take part, so a
        filename never resolves to a file the reader could not download. If
        several visible attachments share a filename, the most recent wins.
        """
        stmt = await self.visible_attachments_stmt(session, project, reader)
        if stmt is None:
            return {}
        att_map: dict[str, str] = {}
        for att_id, filename, _container_type, _container_id in (await session.execute(stmt)).all():
            att_map[filename] = self.download_url(att_id)
        return att_map

    async def build_issue_attachment_map(
        self,
        session: AsyncSession,
        issue_id: int,
        reader: User,
    ) -> dict[str, str]:
        """Build a filename → download URL map of one issue's attachments *reader* may open.

        Used to resolve bare-filename images and links in the issue's
        description and comments. Only files attached to the issue itself or
        to one of its comments take part — never files of another issue — and
        only when *reader* may see the issue (``can_view_container``). Files
        on a private note take part for admins only, as everywhere else. If
        several share a filename, the most recent wins.
        """
        from specivo.models.journal import Journal

        if reader.is_anonymous or not await self.can_view_container(session, reader, "Issue", issue_id):
            return {}

        journal_ids = select(Journal.id).where(Journal.issue_id == issue_id)
        if not reader.is_admin:
            journal_ids = journal_ids.where(Journal.is_private.is_(False))

        rows = await session.execute(
            select(Attachment.id, Attachment.filename)
            .where(
                or_(
                    and_(Attachment.container_type == "Issue", Attachment.container_id == issue_id),
                    and_(Attachment.container_type == "Journal", Attachment.container_id.in_(journal_ids)),
                )
            )
            .order_by(Attachment.created_at.asc(), Attachment.id.asc())
        )
        return {filename: self.download_url(att_id) for att_id, filename in rows.all()}

    async def can_view_container(
        self,
        session: AsyncSession,
        user: User,
        container_type: str,
        container_id: int,
    ) -> bool:
        """Return whether *user* may open attachments of the given container.

        The per-container form of ``visible_attachments_stmt``:

        - ``Issue``: the issue is visible to *user*
          (``IssueService.project_visible_issues_clause``).
        - ``Journal``: an issue comment whose issue is visible and which is
          not a private note (private notes are shown to admins only).
          Comments on wiki pages carry no attachments outside admin access,
          as before.
        - ``WikiPage``: *user* holds ``view_wiki`` in the page's project.

        Admins may open everything, the anonymous user nothing. A missing
        container or an unknown type answers False.
        """
        from specivo.models.issue import Issue
        from specivo.models.journal import Journal
        from specivo.models.wiki import Wiki, WikiPage
        from specivo.services.issue_service import IssueService
        from specivo.services.permission_service import Permission, check_permission

        if user.is_admin:
            return True
        if user.is_anonymous:
            return False

        if container_type == "WikiPage":
            project_id = (
                await session.execute(
                    select(Wiki.project_id)
                    .join(WikiPage, WikiPage.wiki_id == Wiki.id)
                    .where(WikiPage.id == container_id)
                )
            ).scalar_one_or_none()
            if project_id is None:
                return False
            return await check_permission(user, project_id, Permission.VIEW_WIKI, session)

        if container_type == "Journal":
            row = (
                await session.execute(select(Journal.issue_id, Journal.is_private).where(Journal.id == container_id))
            ).one_or_none()
            if row is None or row.issue_id is None or row.is_private:
                return False
            issue_id = row.issue_id
        elif container_type == "Issue":
            issue_id = container_id
        else:
            return False

        project_id = (await session.execute(select(Issue.project_id).where(Issue.id == issue_id))).scalar_one_or_none()
        if project_id is None:
            return False
        project = await session.get(Project, project_id)
        if project is None:
            return False
        visibility = await IssueService().project_visible_issues_clause(session, user, project)
        stmt = select(Issue.id).where(Issue.id == issue_id)
        if visibility is not None:
            stmt = stmt.where(visibility)
        return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def get_visible(self, session: AsyncSession, attachment_id: int, user: User) -> Attachment:
        """Fetch an attachment *user* may open, eagerly loading author.

        Raises ``NotFoundError`` — the same error as for a missing id — when
        the attachment does not exist or its container is hidden from *user*,
        so ids of hidden files cannot be probed.
        """
        attachment = await self.get_by_id(session, attachment_id)
        if not await self.can_view_container(session, user, attachment.container_type, attachment.container_id):
            raise NotFoundError(f"Attachment {attachment_id} not found")
        return attachment

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
                event_type=AuditEvent.ATTACHMENT_UPDATED,
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
