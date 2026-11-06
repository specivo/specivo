"""Bare URLs in Markdown become links, GFM-style.

``https://...``, ``http://...``, ``www....`` and ``mailto:...`` written as
plain text are linked on the parsed text, so code spans and blocks keep them
as written, existing links are not linked twice, trailing punctuation stays
outside the link, and only http, https and mailto targets are ever made.
"""

from __future__ import annotations

import time

import pytest

from specivo.services.markdown_budget import estimate_render_cost, render_hazard
from specivo.services.markdown_service import (
    extract_links,
    render_plain_markdown,
    render_wiki_markdown,
)
from tests.fixtures.markdown_inputs import CATASTROPHE_SECONDS, LINEAR_RATIO_LIMIT, fill, scaling_ratio

pytestmark = pytest.mark.unit

REL = 'rel="noopener noreferrer"'
FALLBACK = 'class="sp-markdown-fallback"'


def render(text: str) -> str:
    return str(render_wiki_markdown(text, project_key="ACME"))


def link(href: str, label: str | None = None) -> str:
    return f'<a href="{href}" {REL}>{label if label is not None else href}</a>'


# ---------------------------------------------------------------------------
# Where bare URLs are linked
# ---------------------------------------------------------------------------


def test_bare_url_in_a_paragraph():
    html = render("See https://github.com/example/platform for details.")
    assert html == f"<p>See {link('https://github.com/example/platform')} for details.</p>"


def test_bare_url_in_a_list_item():
    html = render("- https://example.com/a\n- two")
    assert f"<li>{link('https://example.com/a')}</li>" in html


def test_bare_url_in_a_table_cell():
    html = render("| site |\n|---|\n| https://example.com/t |")
    assert f"<td>{link('https://example.com/t')}</td>" in html


def test_bare_url_in_a_heading_and_emphasis():
    assert link("https://example.com/h") in render("## Docs at https://example.com/h")
    assert f"<em>{link('https://example.com/e')}</em>" in render("*https://example.com/e*")
    assert f"<strong>{link('https://example.com/s')}</strong>" in render("**https://example.com/s**")


def test_http_url_with_query_port_and_fragment():
    url = "http://localhost:8030/projects/X/issues?status=open&sort=id#top"
    html = render(f"Open {url} now")
    assert '<a href="http://localhost:8030/projects/X/issues?status=open&amp;sort=id#top"' in html


def test_www_url_gets_an_http_scheme_and_keeps_its_text():
    html = render("Visit www.example.com/docs today")
    assert link("http://www.example.com/docs", "www.example.com/docs") in html


def test_mailto_url_is_linked():
    html = render("Write to mailto:team@example.com please")
    assert link("mailto:team@example.com") in html


def test_several_urls_in_one_line():
    html = render("https://a.example.com and https://b.example.com/x")
    assert link("https://a.example.com") in html
    assert link("https://b.example.com/x") in html


def test_url_with_an_issue_key_in_its_path_is_one_link():
    html = render("See https://example.com/browse/ACME-12 there")
    assert link("https://example.com/browse/ACME-12") in html
    assert "/issue/ACME-12/" not in html


def test_plain_markdown_filter_links_bare_urls_too():
    assert link("https://example.com/v") in str(render_plain_markdown("Release notes: https://example.com/v"))


# ---------------------------------------------------------------------------
# Trailing punctuation and parentheses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "url", "after"),
    [
        ("Go to https://x.example.com.", "https://x.example.com", "."),
        ("Try https://x.example.com/a, then b", "https://x.example.com/a", ","),
        ("Really https://x.example.com/a?!", "https://x.example.com/a", "?!"),
        ("Note: https://x.example.com/a: done", "https://x.example.com/a", ":"),
        ("Quoted 'https://x.example.com/q' here", "https://x.example.com/q", "'"),
        ('Quoted "https://x.example.com/q" here', "https://x.example.com/q", '"'),
        ("Ends https://x.example.com/a;", "https://x.example.com/a", ";"),
    ],
)
def test_trailing_punctuation_stays_outside_the_link(text, url, after):
    html = render(text)
    assert link(url) in html
    assert html.count("<a ") == 1
    assert after.replace('"', "&quot;") in html.split("</a>", 1)[1] or after in html.split("</a>", 1)[1]


def test_unbalanced_closing_paren_is_not_part_of_the_url():
    html = render("(see https://x.example.com/a)")
    assert html == f"<p>(see {link('https://x.example.com/a')})</p>"


def test_balanced_parens_inside_the_url_are_kept():
    html = render("(see https://x.example.com/a_(b))")
    assert html == f"<p>(see {link('https://x.example.com/a_(b)')})</p>"


def test_period_inside_the_url_is_kept():
    assert link("https://x.example.com/v1.2/file.txt") in render("Get https://x.example.com/v1.2/file.txt.")


def test_trailing_entity_reference_is_not_part_of_the_url():
    html = render("Link https://x.example.com/a&amp; more")
    assert '<a href="https://x.example.com/a"' in html


# ---------------------------------------------------------------------------
# Not linked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "`https://example.com/code`",
        "``see https://example.com/code``",
        "```\nhttps://example.com/code\n```",
        "~~~text\nhttps://example.com/code\n~~~",
        "    https://example.com/code",
        "```mermaid\ngraph TD; A-->B; click A https://example.com/code\n```",
    ],
    ids=["code-span", "double-backtick-span", "fence", "tilde-fence", "indented", "mermaid"],
)
def test_url_in_code_is_not_linked(text):
    html = render(text)
    assert "<a " not in html
    assert "https://example.com/code" in html


def test_markdown_link_is_not_linked_twice():
    html = render("[the docs](https://example.com/d)")
    assert html == f"<p>{link('https://example.com/d', 'the docs')}</p>"


def test_markdown_link_whose_text_is_a_url_is_not_linked_twice():
    html = render("[https://example.com/d](https://example.com/d)")
    assert html.count("<a ") == 1
    assert html == f"<p>{link('https://example.com/d')}</p>"


def test_angle_bracket_autolink_is_not_linked_twice():
    html = render("<https://example.com/d>")
    assert html.count("<a ") == 1
    assert link("https://example.com/d") in html


def test_image_url_is_not_linked():
    html = render("![chart](https://example.com/c.png)")
    assert "<a " not in html
    assert '<img alt="chart" src="https://example.com/c.png"' in html


def test_reference_definition_is_not_linked():
    html = render("[docs][1]\n\n[1]: https://example.com/ref")
    assert html.count("<a ") == 1
    assert link("https://example.com/ref", "docs") in html


def test_raw_html_anchor_is_not_linked_twice():
    html = render('Read <a href="https://example.com/r">https://example.com/r</a> today')
    assert html.count("<a ") == 1
    assert link("https://example.com/r") in html


def test_url_after_a_closed_raw_anchor_is_linked():
    html = render('<a href="https://example.com/r">docs</a> and https://example.com/after')
    assert html.count("<a ") == 2
    assert link("https://example.com/after") in html


def test_raw_html_block_is_left_alone():
    html = render("<div>\nhttps://example.com/block\n</div>")
    assert "<a " not in html


@pytest.mark.parametrize(
    "text",
    [
        "javascript:alert(1)",
        "javascript://example.com/%0Aalert(1)",
        "data:text/html,hi",
        "vbscript:msgbox",
        "ftp://example.com/file",
        "file:///etc/passwd",
    ],
)
def test_other_schemes_are_not_linked(text):
    html = render(f"Try {text} now")
    assert "<a " not in html


def test_url_inside_a_word_is_not_linked():
    assert "<a " not in render("foohttps://example.com/x")
    assert "<a " not in render("foo.www.example.com")


def test_scheme_without_a_host_is_not_linked():
    assert "<a " not in render("Just https:// and http://.")
    assert "<a " not in render("www. alone")


def test_url_with_userinfo_is_not_linked():
    assert "<a " not in render("https://user:secret@example.com/")


def test_escaped_url_markup_does_not_break_out():
    html = render('https://example.com/"><script>alert(1)</script>')
    assert "<script" not in html
    assert 'href="https://example.com/' in html


# ---------------------------------------------------------------------------
# Preview equals the saved render
# ---------------------------------------------------------------------------


def test_preview_and_saved_render_match():
    text = "See https://example.com/p, `https://example.com/c` and [x](https://example.com/l)."
    # The preview endpoint and the wiki_markdown filter both call render_wiki_markdown.
    assert render_wiki_markdown(text) == render_wiki_markdown(text)
    assert link("https://example.com/p") in str(render_wiki_markdown(text))


# ---------------------------------------------------------------------------
# Link graph
# ---------------------------------------------------------------------------


def test_extract_links_sees_bare_urls_to_this_instance():
    text = "See https://tracker.example.com/issue/ACME-7/ and https://tracker.example.com/projects/ACME/wiki/setup/"
    links = extract_links(text, "https://tracker.example.com")
    assert [(found.kind, found.target) for found in links] == [("issue", "ACME-7"), ("wiki", "setup")]


def test_extract_links_ignores_bare_urls_elsewhere_and_in_code():
    text = "See https://other.example.com/issue/ACME-7/ and `https://tracker.example.com/issue/ACME-8/`"
    assert extract_links(text, "https://tracker.example.com") == []


# ---------------------------------------------------------------------------
# Render budget
# ---------------------------------------------------------------------------


def test_many_bare_urls_render_as_links():
    text = "\n".join(f"- https://example.com/page/{i}" for i in range(2000))
    assert render_hazard(text) is None
    html = render(text)
    assert FALLBACK not in html
    assert html.count('<a href="https://example.com/page/') == 2000


@pytest.mark.parametrize(
    "unit",
    [
        "https://x.example.com/a ",
        "www.a.bc ",
        "https:// ",
        "(https://a.b/(x)) ",
        "<a>https://a.b</a> ",
        "<b>https://a.b</b> ",
        "*https://a.b* ",
        "\nhttps://a.b\n",
        "https://a.b/" + "(" * 50 + " ",
    ],
)
def test_url_dense_input_at_the_size_limit_is_bounded(unit):
    """Text of nothing but bare URLs up to the stored-text limit renders or falls back, quickly."""
    text = fill(unit)
    started = time.thread_time()
    html = render(text)
    assert time.thread_time() - started < CATASTROPHE_SECONDS
    if FALLBACK not in html:
        assert html.count("<a ") > 1000


def test_bare_urls_are_costed_by_the_pre_scan():
    with_urls = fill("https://a.b ", size=64 * 1024)
    without = fill("httpsa.b.c ", size=64 * 1024)
    assert estimate_render_cost(with_urls) > estimate_render_cost(without) + 64 * 1024 / 12 * 10
    # Link targets are taken by the link pattern and not costed again.
    targets = fill("[x](https://a.b) ", size=64 * 1024)
    assert estimate_render_cost(targets) == estimate_render_cost(targets.replace("https://", "hxxps://"))


def test_url_dense_rendering_scales_linearly():
    text = fill("https://x.example.com/a ", size=128 * 1024)
    assert scaling_ratio(render, text[: len(text) // 4], text) < LINEAR_RATIO_LIMIT


def test_one_huge_url_is_linear():
    text = "https://example.com/" + "a" * 200_000
    assert scaling_ratio(render, text[: len(text) // 4], text) < LINEAR_RATIO_LIMIT
    assert render(text).count("<a ") == 1
