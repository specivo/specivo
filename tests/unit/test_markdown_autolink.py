"""Unit tests for issue-reference autolink gating in the markdown renderer."""

from __future__ import annotations

from specivo.services.markdown_service import find_issue_ref_candidates, render_wiki_markdown


def test_find_issue_ref_candidates():
    text = "Fixes ACME-1 and BCME-22; not a-ref, not ACME (no number)."
    assert find_issue_ref_candidates(text) == {"ACME-1", "BCME-22"}


def test_find_candidates_empty():
    assert find_issue_ref_candidates("") == set()
    assert find_issue_ref_candidates(None) == set()


def test_known_none_links_all_refs():
    """Default (known_issue_refs=None) preserves original link-everything behaviour."""
    html = str(render_wiki_markdown("See ACME-1 and ZZZ-9"))
    assert '/issue/ACME-1/' in html
    assert '/issue/ZZZ-9/' in html


def test_empty_known_set_links_nothing():
    html = str(render_wiki_markdown("See ACME-1 and ZZZ-9", known_issue_refs=set()))
    assert "/issue/ACME-1/" not in html
    assert "/issue/ZZZ-9/" not in html
    assert "ACME-1" in html  # left as plain text


def test_known_set_links_only_known_refs():
    html = str(render_wiki_markdown("See ACME-1 and ZZZ-9", known_issue_refs={"ACME-1"}))
    assert '/issue/ACME-1/' in html
    assert "/issue/ZZZ-9/" not in html
    assert "ZZZ-9" in html  # plain text
