"""Copy attached files into Specivo and record them.

The bytes live in the source system's storage directory, so this loader is the
one part of the import that touches a filesystem outside the database. Two
consequences shape it.

A file can be missing. Source databases outlive their disks: a row survives a
restore that dropped the files, or the operator pointed the import at the wrong
directory. A row pointing at nothing is worse than no row — it offers a download
that fails — so the attachment is skipped and listed with the path that was
tried, which is also how the operator notices they mounted the wrong volume.

A file can be one this instance would refuse today. The content-type allowlist
governs what people may upload now; a file attached years ago predates that
policy, and dropping it would lose exactly the history the operator asked to
migrate. Those are imported and counted.
"""

from __future__ import annotations

import logging

from specivo.importers.core.backdate import backdate
from specivo.importers.core.ir import ContainerKind, EntityType, IRAttachment
from specivo.importers.core.pipeline import PhaseContext
from specivo.importers.load.user_loader import IMPORT_ACCOUNT_STATE_KEY, ensure_import_account
from specivo.models.attachment import Attachment
from specivo.models.user import User
from specivo.services.attachment_service import ALLOWED_CONTENT_TYPES, AttachmentService

logger = logging.getLogger(__name__)

# Report sections.
NOTE_MISSING_FILES = "attachments_whose_file_was_not_found"
NOTE_UNUSUAL_TYPES = "attachments_of_a_type_this_instance_no_longer_accepts"

# Specivo's polymorphic container names.
_CONTAINER_TYPES: dict[ContainerKind, str] = {
    ContainerKind.ISSUE: "Issue",
    ContainerKind.WIKI_PAGE: "WikiPage",
}

_ENTITY_TYPES: dict[ContainerKind, EntityType] = {
    ContainerKind.ISSUE: EntityType.ISSUE,
    ContainerKind.WIKI_PAGE: EntityType.WIKI_PAGE,
}

_attachment_service = AttachmentService()


async def load_attachments(ctx: PhaseContext, kinds: tuple[ContainerKind, ...] | None = None) -> None:
    """Copy in the files attached to imported issues and wiki pages.

    ``kinds`` narrows the run to one container, so issue attachments can be
    imported with the issues and wiki attachments with the wiki.
    """
    await ensure_import_account(ctx)
    wanted = set(kinds) if kinds else set(_CONTAINER_TYPES)

    for project_ref in ctx.project_refs:
        async for ir in ctx.adapter.extract_attachments(project_ref):
            if ir.container_kind not in wanted:
                continue
            if await ctx.id_map.get(ctx.session, EntityType.ATTACHMENT, ir.source_ref):
                ctx.summary.record_skipped(EntityType.ATTACHMENT)
                continue
            await _load_one(ctx, ir)


async def _load_one(ctx: PhaseContext, ir: IRAttachment) -> Attachment | None:
    """Copy one file in, or explain why it was left behind."""
    container_id = await ctx.id_map.get(ctx.session, _ENTITY_TYPES[ir.container_kind], ir.container_ref)
    if container_id is None:
        ctx.warn(
            "Attachment belongs to something that was not imported; skipped",
            attachment=ir.source_ref,
            filename=ir.filename,
        )
        return None

    try:
        source_path = ctx.adapter.resolve_attachment_path(ir)
    except ValueError as exc:
        ctx.warn("Attachment location could not be resolved; skipped", attachment=ir.source_ref, reason=str(exc))
        return None

    author = await _resolve_author(ctx, ir.author_ref)

    try:
        attachment = await _attachment_service.upload_from_path(
            ctx.session,
            _CONTAINER_TYPES[ir.container_kind],
            container_id,
            source_path,
            ir.filename or source_path.name,
            author,
            description=ir.description,
            content_type=ir.content_type,
            skip_search_index=True,
        )
    except FileNotFoundError:
        # The row outlived its file. A download that fails is worse than an
        # absence the operator has been told about.
        ctx.warn("Attachment file was not found on disk; skipped", attachment=ir.source_ref, path=str(source_path))
        ctx.summary.add_note(NOTE_MISSING_FILES, f"{ir.filename} ({source_path})")
        return None
    except OSError as exc:
        ctx.warn("Attachment file could not be read; skipped", attachment=ir.source_ref, reason=str(exc))
        ctx.summary.add_note(NOTE_MISSING_FILES, f"{ir.filename} ({source_path})")
        return None

    if attachment.content_type not in ALLOWED_CONTENT_TYPES:
        ctx.summary.add_note(NOTE_UNUSUAL_TYPES, f"{attachment.filename} ({attachment.content_type})")

    await backdate(ctx.session, Attachment, attachment.id, created_at=ir.created_at, updated_at=ir.created_at)
    await ctx.id_map.put(ctx.session, EntityType.ATTACHMENT, ir.source_ref, "attachments", attachment.id)
    ctx.summary.record_created(EntityType.ATTACHMENT)
    ctx.tick()
    return attachment


async def load_issue_attachments(ctx: PhaseContext) -> None:
    """Copy in the files attached to imported issues."""
    await load_attachments(ctx, kinds=(ContainerKind.ISSUE,))


async def load_wiki_attachments(ctx: PhaseContext) -> None:
    """Copy in the files attached to imported wiki pages."""
    await load_attachments(ctx, kinds=(ContainerKind.WIKI_PAGE,))


async def _resolve_author(ctx: PhaseContext, author_ref: str | None) -> User:
    """Return who attached the file, falling back to the import account."""
    author_id = await ctx.id_map.get(ctx.session, EntityType.USER, author_ref)
    if author_id is not None:
        author = await ctx.session.get(User, author_id)
        if author is not None:
            return author
    account = ctx.state.get(IMPORT_ACCOUNT_STATE_KEY)
    return account if account is not None else await ensure_import_account(ctx)
