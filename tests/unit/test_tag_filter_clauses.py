"""Unit tests for the search-service real-tag filter clause builder.

``SearchService._tag_exists_clauses`` turns a list of tag names into one
correlated ``EXISTS`` per name (AND logic), parameterized, and addressing the
given link column / entity alias. No database is required.
"""

from __future__ import annotations

from specivo.schemas.search import SearchFilters
from specivo.services.search_service import SearchService


def test_and_logic_one_exists_per_name():
    svc = SearchService()
    params: dict = {}
    sql = svc._tag_exists_clauses(SearchFilters(tag_names=["a", "b"]), params, "issue_id", "i")
    assert sql.count("EXISTS") == 2
    assert ":filter_tag_0" in sql
    assert ":filter_tag_1" in sql
    assert "tl.issue_id = i.id" in sql
    assert params == {"filter_tag_0": "a", "filter_tag_1": "b"}


def test_link_col_and_alias_respected():
    svc = SearchService()
    params: dict = {}
    sql = svc._tag_exists_clauses(SearchFilters(tag_names=["x"]), params, "wiki_page_id", "wp")
    assert "tl.wiki_page_id = wp.id" in sql
    assert "lower(t.name) = lower(:filter_tag_0)" in sql


def test_empty_or_none_yields_nothing():
    svc = SearchService()
    params: dict = {}
    assert svc._tag_exists_clauses(None, params, "issue_id", "i") == ""
    assert svc._tag_exists_clauses(SearchFilters(tag_names=[]), params, "issue_id", "i") == ""
    assert svc._tag_exists_clauses(SearchFilters(tag_names=None), params, "issue_id", "i") == ""
    assert params == {}
