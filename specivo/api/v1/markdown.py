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
from specivo.core.exceptions import AppError
from specivo.core.rate_limit import rate_limit
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.markdown import MarkdownPreviewRequest, MarkdownPreviewResponse
from specivo.services.issue_service import IssueService
from specivo.services.markdown_service import render_wiki_markdown

_issue_svc = IssueService()

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
    :func:`specivo.services.markdown_service.render_wiki_markdown`. Reserved-
    for-future: ``context`` is accepted but currently ignored; ``"wiki"`` and
    ``"issue"`` produce identical output today because both saved-content
    paths use the same filter.
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
    html = render_wiki_markdown(data.text, known_issue_refs=known_issue_refs)
    return MarkdownPreviewResponse(html=str(html))
