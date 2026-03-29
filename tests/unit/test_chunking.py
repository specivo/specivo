"""Unit tests for ChunkingService.

Covers:
- Issue chunking: subject + description -> single chunk
- Wiki page chunking: split by ## headings
- Journal chunking: notes -> single atomic chunk
- Empty content handling
"""

from __future__ import annotations

import pytest

from specivo.services.chunking_service import ChunkingService

pytestmark = pytest.mark.unit


@pytest.fixture
def service() -> ChunkingService:
    return ChunkingService()


class TestIssueChunking:
    def test_issue_single_chunk(self, service: ChunkingService):
        """Issue with subject + description produces a single chunk."""
        chunks = service.chunk_issue("Fix login bug", "Users cannot log in with email")
        assert len(chunks) == 1
        assert "Fix login bug" in chunks[0]
        assert "Users cannot log in with email" in chunks[0]

    def test_issue_no_description(self, service: ChunkingService):
        """Issue with only subject (no description) produces a single chunk."""
        chunks = service.chunk_issue("Deploy new release", None)
        assert len(chunks) == 1
        assert chunks[0] == "Deploy new release"

    def test_issue_empty_description(self, service: ChunkingService):
        """Issue with empty string description produces a single chunk with just subject."""
        chunks = service.chunk_issue("Task title", "")
        assert len(chunks) == 1
        assert chunks[0] == "Task title"


class TestWikiChunking:
    def test_wiki_split_by_headings(self, service: ChunkingService):
        """Wiki page with ## headings is split into sections."""
        text = (
            "Introduction paragraph.\n\n"
            "## Getting Started\n"
            "Start by installing.\n\n"
            "## Configuration\n"
            "Configure the settings.\n\n"
            "## Deployment\n"
            "Deploy to production."
        )
        chunks = service.chunk_wiki_page("Setup Guide", text)
        assert len(chunks) == 4  # intro + 3 sections
        # First chunk includes the title and intro paragraph
        assert "Setup Guide" in chunks[0]
        assert "Introduction paragraph" in chunks[0]
        # Subsequent chunks contain heading-based sections
        assert any("Getting Started" in c for c in chunks)
        assert any("Configuration" in c for c in chunks)
        assert any("Deployment" in c for c in chunks)

    def test_wiki_no_headings(self, service: ChunkingService):
        """Wiki page without headings produces a single chunk with title + text."""
        chunks = service.chunk_wiki_page("Simple Page", "Just plain text content.")
        assert len(chunks) == 1
        assert "Simple Page" in chunks[0]
        assert "Just plain text content." in chunks[0]

    def test_wiki_empty_text(self, service: ChunkingService):
        """Wiki page with empty text produces a single chunk with just the title."""
        chunks = service.chunk_wiki_page("Empty Page", "")
        assert len(chunks) == 1
        assert chunks[0] == "Empty Page"

    def test_wiki_h1_and_h3_headings(self, service: ChunkingService):
        """Wiki page splits on h1, h2, and h3 headings."""
        text = "# Overview\nTop level section.\n\n### Details\nDetailed info here."
        chunks = service.chunk_wiki_page("Doc", text)
        assert len(chunks) == 2
        assert any("Overview" in c for c in chunks)
        assert any("Details" in c for c in chunks)


class TestJournalChunking:
    def test_journal_atomic(self, service: ChunkingService):
        """Journal notes produce a single atomic chunk."""
        chunks = service.chunk_journal("Fixed the bug in the login form")
        assert len(chunks) == 1
        assert chunks[0] == "Fixed the bug in the login form"

    def test_journal_empty(self, service: ChunkingService):
        """Empty journal notes produce no chunks."""
        assert service.chunk_journal("") == []
        assert service.chunk_journal(None) == []
