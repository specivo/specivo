"""``[[Page]]`` wiki links are made by the Markdown parser, never by rewriting the source.

So whatever the renderer shows as code (fenced, indented or inline) or passes
through as a raw HTML block keeps its brackets as written, and every link form
written in ordinary text renders as it did when the source was rewritten.
"""

from __future__ import annotations

import pytest

from specivo.services.markdown_service import render_wiki_markdown

pytestmark = pytest.mark.unit

REL = 'rel="noopener noreferrer"'


def render(text: str, project_key: str = "ACME", known: set[str] | None = None) -> str:
    return str(render_wiki_markdown(text, project_key=project_key, known_issue_refs=known))


def link(slug: str, text: str, project_key: str = "ACME") -> str:
    return f'<a href="/projects/{project_key}/wiki/{slug}/" {REL}>{text}</a>'


def assert_not_linked(html: str) -> None:
    assert "/wiki/" not in html
    assert "[[Home Page]]" in html


# ---------------------------------------------------------------------------
# Code and raw HTML keep their brackets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "```\n[[Home Page]]\n```",
        "~~~\n[[Home Page]]\n~~~",
        "```python\nx = '[[Home Page]]'\n```",
        "````\n```\n[[Home Page]]\n````",
    ],
    ids=["backticks", "tildes", "info-string", "longer-fence"],
)
def test_link_in_fenced_code_is_left_as_written(text):
    assert_not_linked(render(text))


def test_link_in_indented_code_is_left_as_written():
    assert_not_linked(render("Example:\n\n    see [[Home Page]] here\n"))


@pytest.mark.parametrize(
    "text",
    ["Write `[[Home Page]]` to link", "Write ``a ` [[Home Page]]`` to link"],
    ids=["single-backticks", "double-backticks"],
)
def test_link_in_inline_code_is_left_as_written(text):
    html = render(text)
    assert_not_linked(html)
    assert "<code>" in html


def test_link_in_raw_html_block_is_left_as_written():
    html = render('<div class="note">\n[[Home Page]]\n</div>\n\nAfter.')
    assert_not_linked(html)


def test_text_beside_code_is_still_linked():
    html = render("Use `[[Home Page]]` to link [[Home Page]].")
    assert html.count("/wiki/home-page/") == 1
    assert "<code>[[Home Page]]</code>" in html


def test_issue_key_in_code_and_link_in_text_render_together():
    html = render("```\n[[Home Page]] ACME-1\n```\n\n[[Home Page]] and ACME-1")
    assert html.count("/wiki/home-page/") == 1
    assert html.count("/issue/ACME-1/") == 1


# ---------------------------------------------------------------------------
# Every link form renders as before
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("See [[Home]] now.", f"<p>See {link('home', 'Home')} now.</p>"),
        ("[[Getting Started]]", f"<p>{link('getting-started', 'Getting Started')}</p>"),
        ("[[Getting Started|the guide]]", f"<p>{link('getting-started', 'the guide')}</p>"),
        # A project prefix and an anchor are part of the title, as before.
        ("[[PROJ:Page]]", f"<p>{link('projpage', 'PROJ:Page')}</p>"),
        ("[[Page#Section]]", f"<p>{link('pagesection', 'Page#Section')}</p>"),
        ("[[Page#Section|label]]", f"<p>{link('pagesection', 'label')}</p>"),
        ("[[my_page_name]]", f"<p>{link('my-page-name', 'my_page_name')}</p>"),
        ("[[ Spaced  ]]", f"<p>{link('spaced', 'Spaced')}</p>"),
        ("[[Über Seite]]", f"<p>{link('ber-seite', 'Über Seite')}</p>"),
        ("[[!!!]]", f"<p>{link('', '!!!')}</p>"),
        ("[[x|y|z]]", f"<p>{link('x', 'y|z')}</p>"),
        ("[[A]][[B]]", f"<p>{link('a', 'A')}{link('b', 'B')}</p>"),
        ("[[Page]](tail)", f"<p>{link('page', 'Page')}(tail)</p>"),
        # Labels: emphasis renders, markup is shown as written.
        ("[[Page|*bold* label]]", f"<p>{link('page', '<em>bold</em> label')}</p>"),
        ("[[Page|a & b]]", f"<p>{link('page', 'a &amp; b')}</p>"),
        ("[[a<b>c]]", f"<p>{link('abc', 'a&lt;b&gt;c')}</p>"),
        ("[[Page|<script>x</script>]]", f"<p>{link('page', '&lt;script&gt;x&lt;/script&gt;')}</p>"),
        # Escapes hold in the label and the slug is made from what was written.
        ("[[a\\_b]]", f"<p>{link('a-b', 'a_b')}</p>"),
        ("[[a\\b]]", f"<p>{link('ab', 'a\\b')}</p>"),
        # Escaped brackets, an empty label and an unclosed link are not links.
        ("\\[\\[Not]]", "<p>[[Not]]</p>"),
        ("[[Page|]]", "<p>[[Page|]]</p>"),
        ("text [[unclosed", "<p>text [[unclosed</p>"),
    ],
)
def test_link_forms_render_as_before(text, expected):
    assert render(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("**[[Bold Page]]**", f"<strong>{link('bold-page', 'Bold Page')}</strong>"),
        ("- [[List Item]]\n- two", f"<li>{link('list-item', 'List Item')}</li>"),
        ("| a | b |\n|---|---|\n| [[Cell]] | x |", f"<td>{link('cell', 'Cell')}</td>"),
        ("> [[Quoted]]", f"<p>{link('quoted', 'Quoted')}</p>"),
        ("<span>[[Inline Html]]</span>", f"<span>{link('inline-html', 'Inline Html')}</span>"),
    ],
    ids=["emphasis", "list", "table", "quote", "inline-html"],
)
def test_link_inside_other_markup(text, expected):
    assert expected in render(text)


def test_heading_link_keeps_its_anchor():
    html = render("# [[Heading Link]]")
    assert link("heading-link", "Heading Link") in html
    assert 'id="heading-link"' in html


def test_issue_key_in_a_label_is_not_linked_twice():
    html = render("[[Page|ACME-1 label]] and ACME-1")
    assert link("page", "ACME-1 label") in html
    assert html.count("/issue/ACME-1/") == 1


def test_without_a_project_the_link_has_an_empty_key():
    assert render("[[Home]]", project_key="") == f"<p>{link('home', 'Home', project_key='')}</p>"


def test_project_key_builds_the_url():
    assert link("home", "Home", project_key="OPS") in render("[[Home]]", project_key="OPS")
