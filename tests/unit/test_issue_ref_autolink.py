"""Unit tests for issue reference auto-linking in wiki_markdown filter."""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.unit

# Mirror the regex from web/deps.py
_ISSUE_REF_RE = re.compile(r"(?<!\[)(?<!\()(?<!/)\b([A-Z][A-Z0-9]+-\d+)\b")


def _autolink(text: str) -> str:
    """Simulate the issue ref replacement step."""
    return _ISSUE_REF_RE.sub(r"[\1](/issue/\1/)", text)


class TestIssueRefAutolink:
    def test_simple_reference(self):
        assert _autolink("See SPECIVO-49") == "See [SPECIVO-49](/issue/SPECIVO-49/)"

    def test_multiple_references(self):
        result = _autolink("SPECIVO-49 and PERSONAL-1")
        assert "[SPECIVO-49](/issue/SPECIVO-49/)" in result
        assert "[PERSONAL-1](/issue/PERSONAL-1/)" in result

    def test_reference_in_sentence(self):
        result = _autolink("Fixed in SPECIVO-49, see also SPECIVO-50.")
        assert "[SPECIVO-49](/issue/SPECIVO-49/)" in result
        assert "[SPECIVO-50](/issue/SPECIVO-50/)" in result

    def test_no_match_lowercase(self):
        assert _autolink("specivo-49") == "specivo-49"

    def test_no_match_single_letter_project(self):
        """Single uppercase letter is not a valid project key."""
        assert _autolink("A-1") == "A-1"

    def test_already_in_markdown_link(self):
        """Don't double-link references already inside [text](url)."""
        text = "[SPECIVO-49](/issue/SPECIVO-49/)"
        result = _autolink(text)
        # The key inside [] is preceded by [, and inside () by (
        assert result.count("/issue/SPECIVO-49/") == 1

    def test_not_in_url_path(self):
        """Don't match keys that are part of a URL path."""
        text = "See /issue/SPECIVO-49/ for details"
        result = _autolink(text)
        # Should not create a nested link
        assert result.count("[SPECIVO-49]") == 0

    def test_wiki_link_not_affected(self):
        """Wiki links processed first, issue refs should not interfere."""
        # After wiki link processing, [[Page]] becomes [Page](/projects/X/wiki/page/)
        # Issue ref regex should not match inside the resulting markdown link
        text = "[12-Week Program](/projects/PERSONAL/wiki/health-fitness-program/)"
        result = _autolink(text)
        assert result == text  # unchanged

    def test_single_digit_issue(self):
        assert _autolink("PROJ-1") == "[PROJ-1](/issue/PROJ-1/)"

    def test_numeric_in_project_key(self):
        assert _autolink("ABC123X-42") == "[ABC123X-42](/issue/ABC123X-42/)"

    def test_no_text_no_crash(self):
        assert _autolink("") == ""

    def test_no_match_plain_text(self):
        assert _autolink("Hello world") == "Hello world"
