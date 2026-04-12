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
        # Every chunk includes the page title for semantic context
        for chunk in chunks:
            assert "Setup Guide" in chunk
        assert "Introduction paragraph" in chunks[0]
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
        for chunk in chunks:
            assert "Doc" in chunk
        assert any("Overview" in c for c in chunks)
        assert any("Details" in c for c in chunks)

    def test_wiki_code_block_not_split(self, service: ChunkingService):
        """Headings inside fenced code blocks are not split points."""
        text = (
            "## Running Tests\n\n"
            "```bash\n"
            "# All tests\n"
            "$DC exec backend pytest\n"
            "# App tests\n"
            "$DC exec backend pytest subscriptions/\n"
            "```\n\n"
            "## Configuration\n"
            "Set up the config."
        )
        chunks = service.chunk_wiki_page("Testing", text)
        # The code block should stay intact within the "Running Tests" chunk
        code_chunk = [c for c in chunks if "Running Tests" in c]
        assert len(code_chunk) == 1
        assert "# All tests" in code_chunk[0]
        assert "# App tests" in code_chunk[0]
        assert "$DC exec backend pytest" in code_chunk[0]

    def test_wiki_tilde_fence_protected(self, service: ChunkingService):
        """Tilde-fenced code blocks are also protected."""
        text = "## Overview\nIntro.\n\n~~~python\n# This is a comment\ndef foo():\n    pass\n~~~\n"
        chunks = service.chunk_wiki_page("Code", text)
        code_chunk = [c for c in chunks if "Overview" in c]
        assert len(code_chunk) == 1
        assert "# This is a comment" in code_chunk[0]

    def test_wiki_tiny_chunks_merged(self, service: ChunkingService):
        """Sections shorter than MIN_CHUNK_CHARS are merged with neighbours."""
        text = "## A\nShort.\n\n## B\nAlso short.\n\n## C\nTiny."
        chunks = service.chunk_wiki_page("Page", text)
        # All three sections are < 100 chars, should be merged into one chunk
        assert len(chunks) == 1
        assert "Short." in chunks[0]
        assert "Also short." in chunks[0]
        assert "Tiny." in chunks[0]


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
