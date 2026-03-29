"""Pydantic schemas for attachment metadata.

Flat model with optional type-specific sections. The ``source`` field
indicates what produced the metadata (upload, AI description, PDF
extraction, OCR).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ImageMeta(BaseModel):
    """Image-specific metadata."""

    width: int
    height: int
    format: str  # PNG, JPEG, etc.


class PdfMeta(BaseModel):
    """PDF-specific metadata."""

    page_count: int
    title: str | None = None
    author: str | None = None


class TextMeta(BaseModel):
    """Text file metadata."""

    language: str
    line_count: int
    encoding: str = "utf-8"


class ExifMeta(BaseModel):
    """EXIF metadata (extensible)."""

    camera: str | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None


class AttachmentMetadata(BaseModel):
    """Top-level attachment metadata — stored as JSONB in the DB.

    Not a discriminated union: a flat model with optional type-specific
    sections plus free-form content fields.
    """

    schema_version: int = Field(..., ge=1)
    source: Literal["upload", "ai_describe", "pdf_extract", "ocr"]
    generated_at: datetime | None = None
    generated_by: str | None = None  # "user:42", "agent:session-abc", "system:celery"

    # Type-specific (all optional)
    image: ImageMeta | None = None
    pdf: PdfMeta | None = None
    text: TextMeta | None = None
    exif: ExifMeta | None = None

    # Content fields
    ai_description: str | None = None
    extracted_text: str | None = None
