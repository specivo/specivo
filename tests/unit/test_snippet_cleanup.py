"""Unit tests for _clean_snippet in search_service.

Covers wiki link stripping, markdown removal, and <mark> tag preservation.
"""

from __future__ import annotations

import pytest

from specivo.services.search_service import _clean_snippet

pytestmark = pytest.mark.unit


class TestCleanSnippet:
    def test_wiki_link_with_display_text(self):
        """[[target|Display Text]] → Display Text."""
        assert _clean_snippet("see [[seo-strategy|SEO Strategy]] here") == "see SEO Strategy here"

    def test_wiki_link_without_display_text(self):
        """[[page_name]] → page name (underscores to spaces)."""
        assert _clean_snippet("check [[page_name]] now") == "check page name now"

    def test_multiple_wiki_links(self):
        """Multiple wiki links in one snippet are all cleaned."""
        text = "[[seo-strategy|SEO Strategy]] -- [[seo-content-plan-2026|SEO Content Plan 2026]]"
        assert _clean_snippet(text) == "SEO Strategy -- SEO Content Plan 2026"

    def test_bold_markdown_stripped(self):
        """**bold** → bold."""
        assert _clean_snippet("this is **bold text** here") == "this is bold text here"

    def test_italic_markdown_stripped(self):
        """*italic* → italic."""
        assert _clean_snippet("this is *italic text* here") == "this is italic text here"

    def test_mark_tags_preserved(self):
        """<mark> tags from ts_headline are preserved for search highlighting."""
        text = "this has <mark>highlighted</mark> text"
        assert _clean_snippet(text) == "this has <mark>highlighted</mark> text"

    def test_mark_inside_wiki_link(self):
        """<mark> inside wiki link is handled — link resolved, marks preserved."""
        text = "[[<mark>seo</mark>-strategy|SEO <mark>Strategy</mark>]]"
        assert _clean_snippet(text) == "SEO <mark>Strategy</mark>"

    def test_html_escaped(self):
        """HTML in content is escaped to prevent XSS."""
        text = '<img src=x onerror=alert(1)> hello'
        result = _clean_snippet(text)
        assert "<img" not in result
        assert "&lt;img" in result
        assert "hello" in result

    def test_mark_tags_survive_html_escape(self):
        """<mark> tags are preserved even when surrounding content is escaped."""
        text = "<b>bold</b> and <mark>highlighted</mark>"
        result = _clean_snippet(text)
        assert "<mark>highlighted</mark>" in result
        assert "&lt;b&gt;" in result

    def test_none_returns_none(self):
        assert _clean_snippet(None) is None

    def test_empty_string_returns_empty(self):
        assert _clean_snippet("") == ""

    def test_plain_text_unchanged(self):
        text = "just plain text without any markup"
        assert _clean_snippet(text) == text

    def test_combined_markup(self):
        """Mixed wiki links and markdown in a realistic snippet."""
        text = "**Related**: [[seo-strategy|SEO Strategy]], [[seo-scorecard|SEO Scorecard]]"
        assert _clean_snippet(text) == "Related: SEO Strategy, SEO Scorecard"

    def test_partial_link_missing_open_brackets(self):
        """Partial link at snippet start: slug|Display]] → Display."""
        text = "seo-strategy|SEO Strategy]] -- Active strategy document"
        assert _clean_snippet(text) == "SEO Strategy -- Active strategy document"

    def test_partial_link_missing_close_brackets(self):
        """Partial link at snippet end: [[slug|Display → Display."""
        text = "Topical hubs, calendar [[seo-scorecard|scorecard.md"
        assert _clean_snippet(text) == "Topical hubs, calendar scorecard.md"

    def test_heading_markers_stripped(self):
        """## Heading markers are removed from snippets."""
        text = "## Key Decisions Log | Date | Decision"
        assert _clean_snippet(text) == "Key Decisions Log | Date | Decision"
