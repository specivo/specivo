"""Markdown preview API.

A thin authenticated wrapper around the server-side markdown renderer
(`specivo.services.markdown_service`). Editors (textarea, EasyMDE) call this
to render a live preview that exactly matches what the saved content will
look like — shipping a second renderer to the browser would inevitably drift
from the server output (whitespace, list edge cases, the ``KEY-123`` autolink
extension).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import AppError, NotFoundError
from specivo.core.rate_limit import rate_limit
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.markdown import MarkdownPreviewRequest, MarkdownPreviewResponse
from specivo.services.attachment_service import AttachmentService
from specivo.services.issue_service import IssueService
from specivo.services.markdown_service import render_wiki_markdown
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import ProjectService

_attachment_svc = AttachmentService()
_issue_svc = IssueService()
_project_svc = ProjectService()

router = APIRouter(tags=["markdown"])

# 256 KiB body cap. Larger payloads are rejected with 413.
_MAX_TEXT_BYTES = 256 * 1024


@router.post(
    "/markdown/preview/",
    response_model=MarkdownPreviewResponse,
)
async def preview_markdown(
    data: MarkdownPreviewRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(  # noqa: B008
        rate_limit("markdown_preview", max_requests=60, window_seconds=60)
    ),
) -> MarkdownPreviewResponse:
    """Render markdown to sanitized HTML using the server-side renderer.

    The output is byte-identical to what the Jinja ``wiki_markdown`` filter
    produces for the same input — both call into
    :func:`specivo.services.markdown_service.render_wiki_markdown`, given
    the same attachment map: the issue's own attachments for
    ``context="issue"`` with ``issue_id``, the project-wide wiki map for
    ``context="wiki"`` with ``project_key``. Only files the caller may open
    take part; an unknown or hidden container resolves nothing rather than
    erroring, so the endpoint cannot be used to probe for issues.
    """
    if len(data.text.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise AppError(
            code="payload_too_large",
            message=(
                f"Markdown text exceeds the {_MAX_TEXT_BYTES} byte cap. "
                "Split the content into smaller chunks."
            ),
            status_code=413,
            details={"max_bytes": _MAX_TEXT_BYTES},
        )

    known_issue_refs = await _issue_svc.resolve_known_issue_refs(db, data.text)
    project_key, attachment_map = await _preview_attachment_context(db, data, current_user)
    html = render_wiki_markdown(
        data.text,
        project_key,
        attachment_map,
        known_issue_refs=known_issue_refs,
    )
    return MarkdownPreviewResponse(html=str(html))


async def _preview_attachment_context(
    db: AsyncSession,
    data: MarkdownPreviewRequest,
    user: User,
) -> tuple[str, dict[str, str]]:
    """Return the wiki project key and the attachment map the preview renders with.

    Mirrors the saved-content pages: an issue's description and comments
    resolve against that issue's attachments (``build_issue_attachment_map``
    checks that *user* may see the issue), a wiki page against the project
    map (``build_project_attachment_map``, gated here on ``view_wiki``).
    """
    if data.context == "issue" and data.issue_id is not None:
        return "", await _attachment_svc.build_issue_attachment_map(db, data.issue_id, user)

    if data.context == "wiki" and data.project_key:
        try:
            project = await _project_svc.get_by_key(db, data.project_key)
        except NotFoundError:
            return "", {}
        if not await check_permission(user, project.id, Permission.VIEW_WIKI, db):
            return "", {}
        return project.key, await _attachment_svc.build_project_attachment_map(db, project, user)

    return "", {}
