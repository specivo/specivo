"""A ```` ```mermaid ```` fence renders as a marked ``pre`` holding the escaped source.

The browser turns the marker into a diagram; the server keeps the source as
text, so search indexing, exports and MCP reads still see it, and a stored
diagram can never carry markup of its own.
"""

from __future__ import annotations

import re

import pytest

from specivo.services.markdown_service import render_plain_markdown, render_wiki_markdown

pytestmark = pytest.mark.unit


def render(text: str) -> str:
    return str(render_wiki_markdown(text, project_key="ACME"))


def test_mermaid_fence_renders_marker_with_source():
    html = render("```mermaid\ngraph TD\n  A --> B\n```")

    assert html == '<pre class="sp-mermaid">graph TD\n  A --&gt; B\n</pre>'


def test_tilde_fence_and_surrounding_text():
    html = render("Before\n\n~~~mermaid\ngraph LR\n  A --> B\n~~~\n\nAfter")

    assert '<pre class="sp-mermaid">graph LR\n  A --&gt; B\n</pre>' in html
    assert "<p>Before</p>" in html
    assert "<p>After</p>" in html


def test_plain_markdown_filter_renders_marker_too():
    html = str(render_plain_markdown("```mermaid\ngraph TD\n  A --> B\n```"))

    assert '<pre class="sp-mermaid">' in html


def test_other_languages_are_unchanged():
    html = render("```python\nprint('hi')\n```")

    assert "sp-mermaid" not in html
    assert 'class="codehilite"' in html


def test_fence_without_language_is_unchanged():
    html = render("```\ngraph TD\n```")

    assert "sp-mermaid" not in html
    assert 'class="codehilite"' in html


@pytest.mark.parametrize(
    "source",
    [
        "graph TD\n  A[<script>alert(1)</script>] --> B\n",
        'graph TD\n  A["<img src=x onerror=alert(1)>"] --> B\n',
        "graph TD\n  A --> B\n  click A call alert()\n",
    ],
)
def test_markup_in_the_source_stays_text(source: str):
    html = render(f"```mermaid\n{source}```")

    assert "<script" not in html
    assert "<img" not in html
    assert html.startswith('<pre class="sp-mermaid">')
    assert "&lt;" in html or "click A call alert()" in html


def test_closing_pre_in_the_source_cannot_break_out():
    html = render("```mermaid\ngraph TD\n</pre><script>alert(1)</script>\n```")

    assert html.count("</pre>") == 1
    assert "&lt;/pre&gt;&lt;script&gt;" in html


def test_mermaid_fence_inside_a_longer_fence_stays_code():
    html = render("````\n```mermaid\ngraph TD\n```\n````")

    assert "sp-mermaid" not in html
    assert "```mermaid" in html


def test_issue_refs_and_wiki_links_in_a_diagram_are_not_linked():
    html = render("```mermaid\ngraph TD\n  ACME-1 --> B[[Page]]\n```")

    assert "<a " not in html
    assert "ACME-1 --&gt; B[[Page]]" in html


def test_several_diagrams_and_code_blocks():
    text = '```mermaid\ngraph TD\n  A --> B\n```\n\n```python\nx = 1\n```\n\n```mermaid\npie\n  "a" : 1\n```'
    html = render(text)

    assert html.count('<pre class="sp-mermaid">') == 2
    assert html.count('class="codehilite"') == 1


def test_attribute_list_fence_with_mermaid_class():
    html = render("``` { .mermaid }\ngraph TD\n  A --> B\n```")

    assert html == '<pre class="sp-mermaid">graph TD\n  A --&gt; B\n</pre>'


@pytest.mark.parametrize(
    "fence",
    [
        '```mermaid" onload="alert(1)',
        "```mermaid onload=alert(1)",
        "```mermaid><script>alert(1)</script>",
    ],
)
def test_info_string_cannot_inject_attributes(fence: str):
    html = render(f"{fence}\ngraph TD\n  A --> B\n```")

    # Not a mermaid fence at all: the info string is shown as text, never as an attribute.
    assert re.search(r"<[^>]*\sonload=", html) is None
    assert "<script" not in html
    assert 'class="sp-mermaid"' not in html


def test_attribute_list_extras_are_dropped():
    html = render('``` { .mermaid #x onload="alert(1)" style="color:red" data-x=1 }\ngraph TD\n  A --> B\n```')

    assert html == '<pre class="sp-mermaid">graph TD\n  A --&gt; B\n</pre>'


def test_shorter_fence_inside_a_longer_mermaid_fence_stays_source():
    html = render("````mermaid\ngraph TD\n```\nA --> B\n````")

    assert html == '<pre class="sp-mermaid">graph TD\n```\nA --&gt; B\n</pre>'


def test_entities_in_the_source_are_escaped_again():
    html = render("```mermaid\n&lt;script&gt; &amp;\n```")

    assert html == '<pre class="sp-mermaid">&amp;lt;script&amp;gt; &amp;amp;\n</pre>'


def test_raw_html_marker_keeps_only_the_class():
    html = render('<pre class="sp-mermaid" onclick="alert(1)" data-sp-mermaid style="x">graph TD</pre>')

    assert html == '<pre class="sp-mermaid">graph TD</pre>'
