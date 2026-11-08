"""The X-Robots-Tag decision and the page description, without a database."""

from __future__ import annotations

import pytest

from specivo.core.middleware import (
    ANONYMOUS_READER_STATE_KEY,
    ROBOTS_INDEX,
    ROBOTS_NOINDEX,
    ROBOTS_NOINDEX_FOLLOW,
    ROBOTS_STATE_KEY,
    robots_tag_for,
)
from specivo.services.search_engine_service import META_DESCRIPTION_LENGTH, meta_description

pytestmark = pytest.mark.unit


def _scope(tag: str | None, anonymous: bool | None) -> dict:
    state: dict = {}
    if tag is not None:
        state[ROBOTS_STATE_KEY] = tag
    if anonymous is not None:
        state[ANONYMOUS_READER_STATE_KEY] = anonymous
    return {"type": "http", "state": state}


def test_noindex_is_the_default() -> None:
    assert robots_tag_for({"type": "http"}, 200) == ROBOTS_NOINDEX.encode()
    assert robots_tag_for(_scope(None, True), 200) == ROBOTS_NOINDEX.encode()


@pytest.mark.parametrize("tag", [ROBOTS_INDEX, ROBOTS_NOINDEX_FOLLOW])
def test_a_marked_anonymous_200_gets_its_tag(tag: str) -> None:
    assert robots_tag_for(_scope(tag, True), 200) == tag.encode()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 401, 403, 404, 429, 500])
def test_anything_but_a_200_keeps_noindex(status: int) -> None:
    assert robots_tag_for(_scope(ROBOTS_INDEX, True), status) == ROBOTS_NOINDEX.encode()


@pytest.mark.parametrize("anonymous", [False, None])
def test_a_response_not_served_anonymously_keeps_noindex(anonymous: bool | None) -> None:
    assert robots_tag_for(_scope(ROBOTS_INDEX, anonymous), 200) == ROBOTS_NOINDEX.encode()


def test_an_unknown_tag_keeps_noindex() -> None:
    assert robots_tag_for(_scope("all", True), 200) == ROBOTS_NOINDEX.encode()


def test_meta_description_strips_markup() -> None:
    text = "# Title\n\nSee [the docs](https://example.com) and ![img](x.png) **now**.\n\n```\ncode()\n```\n| a | b |"
    assert meta_description(text) == "Title See the docs and img now. a b"


def test_meta_description_is_cut_at_a_word() -> None:
    result = meta_description("word " * 100)
    assert len(result) <= META_DESCRIPTION_LENGTH
    assert result.endswith("word…")


@pytest.mark.parametrize("empty", [None, "", "   ", "```\nonly code\n```"])
def test_meta_description_of_nothing_is_empty(empty: str | None) -> None:
    assert meta_description(empty) == ""
