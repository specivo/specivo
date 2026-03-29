"""Unit tests for ChunkingService.chunk_attachment().

Covers:
- Attachment chunking: filename + description -> single chunk
- Missing or empty description handling
- Long description (still single chunk)
- Return type validation

RED PHASE: chunk_attachment() does not exist yet. These tests
will fail with AttributeError until the method is implemented.
"""

from __future__ import annotations

import pytest

from specivo.services.chunking_service import ChunkingService

pytestmark = pytest.mark.unit


@pytest.fixture
def service() -> ChunkingService:
    return ChunkingService()


class TestAttachmentChunking:
    def test_chunk_attachment_with_description(self, service: ChunkingService):
        """Attachment with filename + description produces a single chunk containing both."""
        chunks = service.chunk_attachment("architecture-diagram.png", "JWT token refresh flow")
        assert len(chunks) == 1
        assert "architecture-diagram.png" in chunks[0]
        assert "JWT token refresh flow" in chunks[0]

    def test_chunk_attachment_without_description(self, service: ChunkingService):
        """Attachment with only filename (no description) produces a single chunk."""
        chunks = service.chunk_attachment("report.pdf", None)
        assert len(chunks) == 1
        assert "report.pdf" in chunks[0]
        # Normalized filename (hyphens/dots -> spaces) should also be present
        assert "report pdf" in chunks[0]

    def test_chunk_attachment_empty_description(self, service: ChunkingService):
        """Attachment with empty string description produces a single chunk with just filename."""
        chunks = service.chunk_attachment("schema.sql", "")
        assert len(chunks) == 1
        assert "schema.sql" in chunks[0]
        assert "schema sql" in chunks[0]

    def test_chunk_attachment_long_description(self, service: ChunkingService):
        """Attachment with a long description (500 chars) still produces a single chunk."""
        long_desc = "A" * 500
        chunks = service.chunk_attachment("big-doc.pdf", long_desc)
        assert len(chunks) == 1
        assert "big-doc.pdf" in chunks[0]
        assert long_desc in chunks[0]

    def test_chunk_returns_list(self, service: ChunkingService):
        """chunk_attachment() returns a list[str]."""
        result = service.chunk_attachment("file.txt", "some description")
        assert isinstance(result, list)
        assert all(isinstance(c, str) for c in result)
