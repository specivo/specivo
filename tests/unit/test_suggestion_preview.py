"""The plain-text preview a "Possibly related" row shows: the start of an issue or page, as one line."""

from __future__ import annotations

import pytest

from specivo.services.link_suggestion_service import plain_preview

pytestmark = pytest.mark.unit


def test_markdown_is_reduced_to_its_words():
    text = (
        "## Goal\n\nSee [[release-runbook|the runbook]], [[testing]] and [the docs](https://example.com).\n"
        "**Bold**, _plain_ and `code` stay as words.\n\n```python\nx = 1\n```\n"
        "- [ ] first task\n- second item\n1. numbered\n> quoted ![diagram](a.png)\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    )
    assert plain_preview(text, 500) == (
        "Goal See the runbook, testing and the docs. Bold, _plain_ and code stay as words. "
        "first task second item numbered quoted a b 1 2"
    )


def test_long_text_is_cut_at_a_word_with_an_ellipsis():
    text = "Suggestions show a short preview of the other item so you can judge them before accepting."
    preview = plain_preview(text, 40)
    assert preview == "Suggestions show a short preview of the…"
    assert len(preview) <= 41


@pytest.mark.parametrize("empty", [None, "", "   \n\n", "```\nonly code\n```"])
def test_nothing_to_show_gives_an_empty_preview(empty):
    assert plain_preview(empty, 100) == ""


def test_html_tags_are_dropped():
    assert plain_preview("<details><summary>Logs</summary>trace</details>", 100) == "Logs trace"
