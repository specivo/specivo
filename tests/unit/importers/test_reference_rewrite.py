"""Unit tests for rewriting source issue references."""

from __future__ import annotations

import pytest

from specivo.importers.load.reference_rewrite import rewrite_text

pytestmark = pytest.mark.unit

MAPPING = {"123": "ACME-15", "7": "OPS-2"}


class TestRewrite:
    def test_known_reference_becomes_a_display_key(self):
        assert rewrite_text("Fixed by #123.", MAPPING) == "Fixed by ACME-15."

    def test_several_references(self):
        assert rewrite_text("See #123 and #7.", MAPPING) == "See ACME-15 and OPS-2."

    def test_reference_at_the_start_of_a_line(self):
        assert rewrite_text("#123 is the cause", MAPPING) == "ACME-15 is the cause"

    def test_unknown_reference_is_left_alone(self):
        """It may be a version, a quantity, or an issue deleted years ago."""
        assert rewrite_text("See #999.", MAPPING) == "See #999."

    def test_cross_project_reference_uses_the_other_key(self):
        assert rewrite_text("Blocked by #7", MAPPING) == "Blocked by OPS-2"

    def test_text_without_references_is_unchanged(self):
        assert rewrite_text("Nothing to do here.", MAPPING) == "Nothing to do here."

    def test_empty_mapping_changes_nothing(self):
        assert rewrite_text("See #123.", {}) == "See #123."

    def test_a_longer_number_is_not_partially_matched(self):
        assert rewrite_text("See #1234.", MAPPING) == "See #1234."

    def test_colour_literal_is_not_a_reference(self):
        assert rewrite_text("Use #123456 for the border.", MAPPING) == "Use #123456 for the border."

    def test_html_entity_is_not_a_reference(self):
        assert rewrite_text("&#123;", MAPPING) == "&#123;"


class TestCodeBlocks:
    def test_fenced_block_is_left_alone(self):
        """A shell prompt or colour literal in a sample must survive."""
        text = "Before #123\n\n```\ncurl -H 'X: #123'\n```\n\nAfter #123"
        result = rewrite_text(text, MAPPING)
        assert "curl -H 'X: #123'" in result
        assert result.startswith("Before ACME-15")
        assert result.endswith("After ACME-15")

    def test_tilde_fence_is_also_respected(self):
        text = "~~~\n#123\n~~~"
        assert rewrite_text(text, MAPPING) == text

    def test_two_blocks_with_text_between(self):
        text = "```\n#123\n```\n#123\n```\n#123\n```"
        result = rewrite_text(text, MAPPING)
        assert result.count("#123") == 2
        assert result.count("ACME-15") == 1
