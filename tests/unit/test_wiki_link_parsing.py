"""Unit tests for wiki link parsing.

Covers:
- Simple [[Page_Name]] links
- Links with display text [[Page|Show This]]
- Multiple links in one text
- Deduplication by slug
- Slug normalization (spaces, underscores, case)
- Empty/invalid links
- Multi-line content
"""

from __future__ import annotations

import pytest

from specivo.services.wiki_link_service import WikiLinkService

pytestmark = pytest.mark.unit


@pytest.fixture
def service() -> WikiLinkService:
    return WikiLinkService()


class TestParseLinks:
    def test_simple_link(self, service: WikiLinkService):
        """[[Page_Name]] is parsed to slug with no display text."""
        result = service.parse_links("See [[Page_Name]]")
        assert result == [("page-name", None)]

    def test_link_with_display_text(self, service: WikiLinkService):
        """[[Page|Show This]] returns slug and display text."""
        result = service.parse_links("[[Page|Show This]]")
        assert result == [("page", "Show This")]

    def test_multiple_links(self, service: WikiLinkService):
        """Multiple links in one string are all extracted."""
        result = service.parse_links("[[A]] and [[B]]")
        assert result == [("a", None), ("b", None)]

    def test_no_links(self, service: WikiLinkService):
        """Plain text without links returns empty list."""
        result = service.parse_links("plain text")
        assert result == []

    def test_dedup_same_target(self, service: WikiLinkService):
        """Duplicate links to the same slug are deduplicated."""
        result = service.parse_links("[[A]] then [[A]] again")
        assert result == [("a", None)]

    def test_spaces_become_hyphens(self, service: WikiLinkService):
        """Spaces in link target are converted to hyphens in the slug."""
        result = service.parse_links("[[My Page]]")
        assert result == [("my-page", None)]

    def test_underscores_become_hyphens(self, service: WikiLinkService):
        """Underscores in link target are converted to hyphens in the slug."""
        result = service.parse_links("[[My_Page]]")
        assert result == [("my-page", None)]

    def test_empty_link_ignored(self, service: WikiLinkService):
        """Empty [[]] links are ignored."""
        result = service.parse_links("[[]]")
        assert result == []

    def test_link_in_multiline(self, service: WikiLinkService):
        """Links are found across multiple lines of markdown."""
        text = "# Heading\n\nSee [[Page_One]] for details.\n\nAlso read [[Page_Two|the other page]]."
        result = service.parse_links(text)
        assert result == [("page-one", None), ("page-two", "the other page")]
