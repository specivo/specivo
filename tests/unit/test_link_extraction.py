"""Unit tests for ``markdown_service.extract_links``: the links the renderer makes of a text.

The entity link graph is built from these, so they must be exactly the links a
reader sees: ``[[Page]]`` wiki links, ``KEY-123`` issue references, links to
issues, wiki pages and attachments of this instance, and bare attachment
filenames; never anything in code, raw HTML or (for issue keys) link text.
"""

from __future__ import annotations

import pytest

from specivo.services.markdown_service import FoundLink, classify_link_target, extract_links

pytestmark = pytest.mark.unit

ORIGIN = "https://tracker.example.com"


def _kinds(text: str) -> list[tuple[str, str]]:
    return [(link.kind, link.target) for link in extract_links(text, ORIGIN)]


class TestWikiLinks:
    def test_simple_link(self):
        assert extract_links("See [[Page_Name]]") == [FoundLink("wiki", "page-name")]

    def test_link_with_display_text(self):
        assert extract_links("[[Page|Show This]]") == [FoundLink("wiki", "page", label="Show This")]

    def test_multiple_links_in_order_duplicates_kept(self):
        """The extractor reports every link; the service de-duplicates."""
        assert _kinds("[[A]] and [[B]] then [[A]]") == [("wiki", "a"), ("wiki", "b"), ("wiki", "a")]

    def test_slug_normalisation(self):
        assert _kinds("[[My Page]] [[My_Other_Page]]") == [("wiki", "my-page"), ("wiki", "my-other-page")]

    def test_empty_link_is_not_a_link(self):
        assert extract_links("[[]]") == []

    def test_link_in_multiline(self):
        text = "# Heading\n\nSee [[Page_One]] for details.\n\nAlso read [[Page_Two|the other page]]."
        assert extract_links(text) == [
            FoundLink("wiki", "page-one"),
            FoundLink("wiki", "page-two", label="the other page"),
        ]

    def test_label_reads_as_written(self):
        assert extract_links("[[Page|a <b> \\_c]]") == [FoundLink("wiki", "page", label="a <b> \\_c")]

    def test_labelled_links_in_a_table_are_found(self):
        """The label's ``|`` is part of the link, not a column break."""
        text = "| Page | What |\n|---|---|\n| [[Page_One|One]] | first |\n| [[Page Two|Two]] | second |\n"
        assert extract_links(text) == [
            FoundLink("wiki", "page-one", label="One"),
            FoundLink("wiki", "page-two", label="Two"),
        ]

    def test_no_links(self):
        assert extract_links("plain text") == []
        assert extract_links("") == []
        assert extract_links(None) == []


class TestIssueReferences:
    def test_issue_key(self):
        assert _kinds("Fixed in ACME-12, see also OPS-3.") == [("issue", "ACME-12"), ("issue", "OPS-3")]

    def test_key_in_link_text_is_the_link_not_a_second_reference(self):
        """``[ACME-5](/issue/ACME-5/)`` is one link, whose label is the key itself."""
        assert extract_links("[ACME-5](/issue/ACME-5/)") == [FoundLink("issue", "ACME-5")]

    def test_key_after_a_slash_is_part_of_a_path(self):
        assert _kinds("path/ACME-1 here") == []


class TestNotLinks:
    def test_code_and_raw_html_are_not_links(self):
        """Only the links the page renders count; code and raw HTML show theirs as written."""
        text = (
            "See [[Real Page]] and ACME-1.\n\n"
            "```\n[[Fenced]] ACME-2\n```\n\n"
            "    [[Indented]] ACME-3\n\n"
            "Inline `[[Inline]] ACME-4` code.\n\n"
            "<div>\n[[Raw Html]] ACME-5\n</div>\n"
        )
        assert _kinds(text) == [("wiki", "real-page"), ("issue", "ACME-1")]

    def test_text_over_the_render_budget_is_scanned_as_written(self):
        """The page shows such text as plain text; its links still make the graph, code and all."""
        text = "`[[In Code]] ACME-9` " + "[" * 30000 + " [[After]]"
        assert _kinds(text) == [("wiki", "in-code"), ("wiki", "after"), ("issue", "ACME-9")]


class TestInstanceLinks:
    def test_relative_issue_and_wiki_links(self):
        text = "[a](/issue/OPS-3/) [b](/projects/OPS/issues/OPS-4/) [c](/projects/ops/wiki/run-book/history/)"
        assert extract_links(text, ORIGIN) == [
            FoundLink("issue", "OPS-3", label="a"),
            FoundLink("issue", "OPS-4", label="b"),
            FoundLink("wiki", "run-book", label="c", project_key="OPS"),
        ]

    def test_absolute_links_on_this_instance_only(self):
        text = (
            "[here](https://tracker.example.com/issue/OPS-3/) "
            "[elsewhere](https://other.example.org/issue/OPS-4/) "
            "<https://TRACKER.example.com/projects/OPS/wiki/faq/>"
        )
        assert extract_links(text, ORIGIN) == [
            FoundLink("issue", "OPS-3", label="here"),
            FoundLink("wiki", "faq", project_key="OPS"),
        ]

    def test_absolute_links_need_a_known_origin(self):
        assert extract_links("[x](https://tracker.example.com/issue/OPS-3/)") == []

    def test_attachment_url(self):
        assert _kinds("[file](/api/v1/attachments/7/download/) [f](/attachments/9/download/)") == [("attachment", "9")]

    def test_wiki_route_segments_are_not_pages(self):
        assert _kinds("[t](/projects/OPS/wiki/trash/) [n](/projects/OPS/wiki/new/)") == []

    def test_bare_filenames_are_attachments(self):
        assert extract_links("[the spec](my%20spec.pdf) ![chart](chart.png)") == [
            FoundLink("file", "my spec.pdf", label="the spec"),
            FoundLink("file", "chart.png", label="chart"),
        ]

    def test_other_targets_are_ignored(self):
        assert _kinds("[a](mailto:x@example.com) [b](#section) [c](https://example.org/) [d](/projects/)") == []


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/issue/ACME-1/", FoundLink("issue", "ACME-1")),
        ("/issue/ACME-1", FoundLink("issue", "ACME-1")),
        ("/issue/ACME-1/edit/", FoundLink("issue", "ACME-1")),
        ("/issue/acme-1/", None),
        ("/projects/ACME/wiki/Some-Page/", FoundLink("wiki", "some-page", project_key="ACME")),
        ("https://tracker.example.com:443/issue/ACME-1/", None),
        ("http://tracker.example.com/issue/ACME-1/", None),
        ("report.csv", FoundLink("file", "report.csv")),
        ("dir/report.csv", None),
    ],
)
def test_classify_link_target(target: str, expected: FoundLink | None):
    assert classify_link_target(target, ORIGIN) == expected
