"""Unit tests for attachment metadata Pydantic schemas and multi-chunk indexing.

Covers:
- Pydantic metadata schema validation (specivo.schemas.attachment_metadata)
- Multi-chunk support in chunk_attachment() with extracted_text parameter

RED PHASE: The metadata schemas and extracted_text parameter don't exist yet.
These tests will fail with ImportError / TypeError until Phase 2 is implemented.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from specivo.services.chunking_service import ChunkingService

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _import_metadata_schemas():
    """Import metadata schemas — will fail until module is created."""
    from specivo.schemas.attachment_metadata import (
        AttachmentMetadata,
        ImageMeta,
        PdfMeta,
        TextMeta,
    )

    return AttachmentMetadata, ImageMeta, PdfMeta, TextMeta


# ---------------------------------------------------------------------------
# Tests: Pydantic metadata schema validation
# ---------------------------------------------------------------------------


class TestMetadataSchemaValidation:
    """Validate the AttachmentMetadata Pydantic model and its sub-models."""

    def test_image_metadata_valid(self):
        """Valid image metadata with width, height, format is accepted."""
        AttachmentMetadata, *_ = _import_metadata_schemas()

        meta = AttachmentMetadata(
            schema_version=1,
            source="upload",
            generated_at="2026-03-29T10:00:00Z",
            generated_by="user:42",
            image={"width": 1920, "height": 1080, "format": "PNG"},
        )
        assert meta.schema_version == 1
        assert meta.source == "upload"
        assert meta.image is not None
        assert meta.image.width == 1920
        assert meta.image.height == 1080
        assert meta.image.format == "PNG"

    def test_pdf_metadata_valid(self):
        """Valid PDF metadata with page_count, title, author is accepted."""
        AttachmentMetadata, *_ = _import_metadata_schemas()

        meta = AttachmentMetadata(
            schema_version=1,
            source="pdf_extract",
            generated_at="2026-03-29T10:00:00Z",
            generated_by="system:celery",
            pdf={"page_count": 12, "title": "Auth Guide", "author": "Boris S."},
        )
        assert meta.pdf is not None
        assert meta.pdf.page_count == 12
        assert meta.pdf.title == "Auth Guide"
        assert meta.pdf.author == "Boris S."

    def test_text_metadata_valid(self):
        """Valid text metadata with language, line_count, encoding is accepted."""
        AttachmentMetadata, *_ = _import_metadata_schemas()

        meta = AttachmentMetadata(
            schema_version=1,
            source="upload",
            generated_at="2026-03-29T10:00:00Z",
            generated_by="user:7",
            text={"language": "python", "line_count": 247, "encoding": "utf-8"},
        )
        assert meta.text is not None
        assert meta.text.language == "python"
        assert meta.text.line_count == 247
        assert meta.text.encoding == "utf-8"

    def test_metadata_with_ai_description(self):
        """Metadata with ai_description field is accepted and stored."""
        AttachmentMetadata, *_ = _import_metadata_schemas()

        meta = AttachmentMetadata(
            schema_version=1,
            source="ai_describe",
            generated_at="2026-03-29T10:00:00Z",
            generated_by="agent:claude-session-abc",
            image={"width": 800, "height": 600, "format": "JPEG"},
            ai_description="A flowchart showing JWT token refresh with three decision nodes",
        )
        assert meta.ai_description == "A flowchart showing JWT token refresh with three decision nodes"

    def test_metadata_with_extracted_text(self):
        """Metadata with extracted_text field is accepted and stored."""
        AttachmentMetadata, *_ = _import_metadata_schemas()

        meta = AttachmentMetadata(
            schema_version=1,
            source="pdf_extract",
            generated_at="2026-03-29T10:00:00Z",
            generated_by="system:celery",
            pdf={"page_count": 5, "title": "Guide"},
            extracted_text="Section 3.2 describes the refresh token rotation...",
        )
        assert meta.extracted_text == "Section 3.2 describes the refresh token rotation..."

    def test_metadata_schema_version_required(self):
        """schema_version is required — omitting it raises ValidationError."""
        AttachmentMetadata, *_ = _import_metadata_schemas()

        with pytest.raises(ValidationError) as exc_info:
            AttachmentMetadata(
                source="upload",
                generated_at="2026-03-29T10:00:00Z",
                generated_by="user:1",
            )
        errors = exc_info.value.errors()
        field_names = [e["loc"][0] for e in errors]
        assert "schema_version" in field_names

    def test_metadata_source_required(self):
        """source is required — omitting it raises ValidationError."""
        AttachmentMetadata, *_ = _import_metadata_schemas()

        with pytest.raises(ValidationError) as exc_info:
            AttachmentMetadata(
                schema_version=1,
                generated_at="2026-03-29T10:00:00Z",
                generated_by="user:1",
            )
        errors = exc_info.value.errors()
        field_names = [e["loc"][0] for e in errors]
        assert "source" in field_names

    def test_metadata_invalid_source_rejected(self):
        """Invalid source value (not in allowed set) raises ValidationError."""
        AttachmentMetadata, *_ = _import_metadata_schemas()

        with pytest.raises(ValidationError):
            AttachmentMetadata(
                schema_version=1,
                source="invalid_source",
                generated_at="2026-03-29T10:00:00Z",
                generated_by="user:1",
            )

    def test_metadata_exif_optional(self):
        """EXIF data is optional on image metadata — omitting it is fine."""
        AttachmentMetadata, *_ = _import_metadata_schemas()

        # Without exif
        meta_no_exif = AttachmentMetadata(
            schema_version=1,
            source="upload",
            generated_at="2026-03-29T10:00:00Z",
            generated_by="user:42",
            image={"width": 1920, "height": 1080, "format": "PNG"},
        )
        assert meta_no_exif.exif is None

        # With exif
        meta_with_exif = AttachmentMetadata(
            schema_version=1,
            source="upload",
            generated_at="2026-03-29T10:00:00Z",
            generated_by="user:42",
            image={"width": 1920, "height": 1080, "format": "PNG"},
            exif={"camera": "Canon EOS R5", "gps_lat": 13.7563, "gps_lon": 100.5018},
        )
        assert meta_with_exif.exif is not None
        assert meta_with_exif.exif.camera == "Canon EOS R5"

    def test_metadata_generated_by_format(self):
        """generated_by accepts 'user:42', 'agent:claude-session-abc', 'system:celery'."""
        AttachmentMetadata, *_ = _import_metadata_schemas()

        for generated_by in ["user:42", "agent:claude-session-abc", "system:celery"]:
            meta = AttachmentMetadata(
                schema_version=1,
                source="upload",
                generated_at="2026-03-29T10:00:00Z",
                generated_by=generated_by,
            )
            assert meta.generated_by == generated_by


# ---------------------------------------------------------------------------
# Tests: multi-chunk with extracted text
# ---------------------------------------------------------------------------


@pytest.fixture
def service() -> ChunkingService:
    return ChunkingService()


class TestMultiChunkExtractedText:
    """chunk_attachment() with extracted_text parameter produces multiple chunks."""

    def test_chunk_attachment_with_extracted_text(self, service: ChunkingService):
        """filename + description + extracted_text produces multiple chunks."""
        chunks = service.chunk_attachment(
            "auth-guide.pdf",
            "Authentication system documentation",
            extracted_text="Section 1 covers OAuth2 basics. Section 2 describes JWT tokens.",
        )
        assert len(chunks) >= 2
        # All chunks together should contain the extracted text content
        all_text = " ".join(chunks)
        assert "OAuth2" in all_text
        assert "JWT tokens" in all_text

    def test_chunk_attachment_extracted_text_long(self, service: ChunkingService):
        """5000 char extracted text splits into multiple chunks (each approx 1000 chars)."""
        long_text = ("This is a paragraph about authentication mechanisms. " * 20 + "\n\n") * 5
        assert len(long_text) > 4000  # sanity check

        chunks = service.chunk_attachment(
            "long-doc.pdf",
            "Lengthy document",
            extracted_text=long_text,
        )
        # chunk[0] is filename+description, chunks[1:] are from extracted_text
        # With ~5000 chars of extracted text, expect at least 5 chunks total
        assert len(chunks) >= 5

        # Each chunk should be roughly within 1200 chars (some tolerance for splitting)
        for chunk in chunks:
            assert len(chunk) <= 1200

    def test_chunk_attachment_first_chunk_is_filename_description(self, service: ChunkingService):
        """chunk[0] is always filename + description, not extracted text."""
        chunks = service.chunk_attachment(
            "report.pdf",
            "Quarterly financial report",
            extracted_text="Revenue grew by 15% in Q3. Operating margins improved to 22%.",
        )
        assert len(chunks) >= 2
        assert "report.pdf" in chunks[0]
        assert "Quarterly financial report" in chunks[0]

    def test_chunk_attachment_subsequent_chunks_are_extracted_text(self, service: ChunkingService):
        """chunks[1:] contain content from extracted_text."""
        extracted = "Revenue grew by 15% in Q3. Operating margins improved to 22%."
        chunks = service.chunk_attachment(
            "report.pdf",
            "Quarterly financial report",
            extracted_text=extracted,
        )
        assert len(chunks) >= 2
        # The extracted text should appear in chunks after the first one
        subsequent_text = " ".join(chunks[1:])
        assert "Revenue grew" in subsequent_text

    def test_chunk_attachment_no_extracted_text_single_chunk(self, service: ChunkingService):
        """Without extracted_text, still produces a single chunk (backward compat)."""
        chunks = service.chunk_attachment("diagram.png", "Architecture overview")
        assert len(chunks) == 1
        assert "diagram.png" in chunks[0]
        assert "Architecture overview" in chunks[0]
