"""Pydantic schemas for markdown preview."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MarkdownPreviewRequest(BaseModel):
    """Request body for the markdown preview endpoint.

    ``context`` is reserved for future divergence between issue/wiki rendering.
    Today both contexts use the shared ``wiki_markdown`` pipeline (the same
    one that renders saved issue descriptions, comments, and wiki pages),
    so the value is accepted-but-ignored and both produce identical output.
    """

    text: str = Field(default="", description="Raw markdown source")
    context: Literal["wiki", "issue"] = Field(
        default="issue",
        description="Reserved for future divergence; both values render identically today.",
    )


class MarkdownPreviewResponse(BaseModel):
    """Response body for the markdown preview endpoint."""

    html: str = Field(description="Sanitized HTML produced by the server-side renderer")
