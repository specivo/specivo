"""Pydantic schemas for markdown preview."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MarkdownPreviewRequest(BaseModel):
    """Request body for the markdown preview endpoint.

    ``context`` names the kind of content being edited, and the optional
    container fields say where it will be stored, so the preview resolves
    bare-filename images and links (``![chart](chart.png)``,
    ``[the spec](spec.pdf)``) exactly as the saved content will:

    - ``context="issue"`` with ``issue_id``: that issue's attachments;
    - ``context="wiki"`` with ``project_key``: the project-wide map wiki
      pages use (wiki ``[[Page]]`` links also get the project in their URL).

    Only attachments the caller may open take part. Without a container, no
    filename is resolved.
    """

    text: str = Field(default="", description="Raw markdown source")
    context: Literal["wiki", "issue"] = Field(
        default="issue",
        description="Kind of content being edited: an issue description/comment or a wiki page.",
    )
    issue_id: int | None = Field(
        default=None,
        description="Issue the text belongs to (issue context); resolves that issue's attachments.",
    )
    project_key: str | None = Field(
        default=None,
        max_length=64,
        description="Project whose wiki the text belongs to (wiki context); resolves wiki attachments.",
    )


class MarkdownPreviewResponse(BaseModel):
    """Response body for the markdown preview endpoint."""

    html: str = Field(description="Sanitized HTML produced by the server-side renderer")
