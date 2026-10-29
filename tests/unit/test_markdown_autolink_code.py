"""Issue-key autolinking leaves code, raw HTML blocks and link text alone.

The linker runs on what Python-Markdown parsed, so whatever the renderer shows
as code (fenced, indented or inline) keeps its keys as written, a raw HTML block
is passed through untouched, and a key that is already link text is not linked
a second time.
"""

from __future__ import annotations

import pytest

from specivo.services.markdown_service import render_wiki_markdown

pytestmark = pytest.mark.unit

LINK = '<a href="/issue/ACME-1/" rel="noopener noreferrer">ACME-1</a>'


def render(text: str, known: set[str] | None = None) -> str:
    return str(render_wiki_markdown(text, project_key="ACME", known_issue_refs=known))


def assert_not_linked(html: str) -> None:
    assert "/issue/ACME-1/" not in html
    assert "[ACME-1]" not in html
    assert "ACME-1" in html


@pytest.mark.parametrize(
    "text",
    [
        "```\nACME-1\n```",
        "~~~\nACME-1\n~~~",
        "```python\nprint('ACME-1')\n```",
        # A longer fence is closed only by a fence at least as long, so the
        # inner ``` line is code and the key after it is still inside the block.
        "````\n```\nACME-1\n````",
        "~~~~\n~~~\nACME-1\n~~~~",
    ],
    ids=["backticks", "tildes", "info-string", "longer-backtick-fence", "longer-tilde-fence"],
)
def test_key_in_fenced_code_is_not_linked(text):
    assert_not_linked(render(text))


def test_key_in_indented_code_is_not_linked():
    assert_not_linked(render("Example:\n\n    git commit -m 'ACME-1 fix'\n"))


@pytest.mark.parametrize(
    "text",
    ["Run `ACME-1` now", "Run ``a ` ACME-1`` now"],
    ids=["single-backticks", "double-backticks"],
)
def test_key_in_inline_code_is_not_linked(text):
    assert_not_linked(render(text))


def test_key_in_raw_html_block_is_left_alone():
    """A raw HTML block is passed through as written: no Markdown link source in it."""
    html = render("<table><tr><td>ACME-1</td></tr></table>")
    assert "<td>ACME-1</td>" in html
    assert_not_linked(html)


def test_key_in_inline_html_is_linked_as_html():
    html = render("Text <span>ACME-1</span> here")
    assert f"<span>{LINK}</span>" in html
    assert "[ACME-1]" not in html


def test_key_in_link_text_is_not_linked_again():
    html = render("[see ACME-1](https://example.com/)")
    assert html.count("<a ") == 1
    assert "/issue/ACME-1/" not in html


def test_keys_outside_code_are_still_linked():
    text = "\n\n".join(
        [
            "# Plan ACME-1",
            "Fixed in ACME-1, see `ACME-2`.",
            "*ACME-1* and **ACME-1**",
            "| Issue |\n|---|\n| ACME-1 |",
            "```\nACME-2\n```",
        ]
    )
    html = render(text)
    assert html.count(LINK) == 5
    assert "/issue/ACME-2/" not in html


def test_text_after_a_fence_is_linked():
    html = render("```\nACME-2\n```\n\nAfter ACME-1")
    assert LINK in html
    assert "/issue/ACME-2/" not in html


def test_known_refs_still_filter_links():
    html = render("ACME-1 and ZZZ-9, `ACME-1`", known={"ACME-1"})
    assert html.count(LINK) == 1
    assert "<code>ACME-1</code>" in html
    assert "/issue/ZZZ-9/" not in html
    assert "ZZZ-9" in html
