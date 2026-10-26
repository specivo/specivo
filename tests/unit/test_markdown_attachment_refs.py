"""Unit tests for bare-filename attachment resolution in the Markdown renderer.

Images and plain links resolve the same way: the target, URL-decoded, must be
an exact key of the attachment map the caller built. Anything that is not a
bare filename, and any unknown name, is rendered exactly as written.
"""

from __future__ import annotations

import pytest

from specivo.services.markdown_service import render_wiki_markdown
from tests.fixtures.markdown_inputs import CATASTROPHE_SECONDS, LINEAR_RATIO_LIMIT, cpu_time, scaling_ratio

pytestmark = pytest.mark.unit

FALLBACK = 'class="sp-markdown-fallback"'

ATTACHMENTS = {
    "spec.pdf": "/api/v1/attachments/1/download/",
    "chart.png": "/api/v1/attachments/2/download/",
    "release notes.pdf": "/api/v1/attachments/3/download/",
    "report (final).pdf": "/api/v1/attachments/4/download/",
}


def render(text: str, attachment_map: dict | None = ATTACHMENTS) -> str:
    return str(render_wiki_markdown(text, project_key="ACME", attachment_map=attachment_map))


def test_pdf_link_resolves():
    assert '<a href="/api/v1/attachments/1/download/" rel="noopener noreferrer">the spec</a>' in render(
        "See [the spec](spec.pdf)."
    )


def test_image_still_resolves():
    assert 'src="/api/v1/attachments/2/download/"' in render("![chart](chart.png)")


def test_link_title_is_kept():
    html = render('[spec](spec.pdf "The spec")')
    assert 'href="/api/v1/attachments/1/download/"' in html
    assert 'title="The spec"' in html


def test_unknown_filename_is_left_as_written():
    assert 'href="missing.pdf"' in render("[x](missing.pdf)")
    assert 'src="missing.png"' in render("![x](missing.png)")


def test_match_is_case_sensitive():
    assert 'href="Spec.pdf"' in render("[x](Spec.pdf)")


@pytest.mark.parametrize(
    ("source", "href"),
    [
        ("[x](https://example.com/spec.pdf)", "https://example.com/spec.pdf"),
        ("[x](http://example.com/spec.pdf)", "http://example.com/spec.pdf"),
        ("[x](docs/spec.pdf)", "docs/spec.pdf"),
        ("[x](./spec.pdf)", "./spec.pdf"),
        ("[x](/spec.pdf)", "/spec.pdf"),
        ("[x](#spec.pdf)", "#spec.pdf"),
        ("[x](mailto:spec.pdf)", "mailto:spec.pdf"),
    ],
)
def test_non_bare_targets_are_untouched(source, href):
    """Even when the last segment names a known attachment."""
    assert f'href="{href}"' in render(source)


@pytest.mark.parametrize(
    "source",
    [
        "[notes](<release notes.pdf>)",
        "[notes](release%20notes.pdf)",
        "[notes](release notes.pdf)",
        "[report](<report (final).pdf>)",
        "[report](report%20%28final%29.pdf)",
    ],
)
def test_spaces_and_url_encoding_resolve(source):
    html = render(source)
    assert 'href="/api/v1/attachments/3/download/"' in html or 'href="/api/v1/attachments/4/download/"' in html


def test_nothing_resolves_without_a_map():
    assert 'href="spec.pdf"' in render("[x](spec.pdf)", attachment_map=None)
    assert 'href="spec.pdf"' in render("[x](spec.pdf)", attachment_map={})


def test_wiki_links_and_issue_refs_are_unaffected():
    html = render("[[Spec]] and ACME-1 and [the spec](spec.pdf)")
    assert 'href="/projects/ACME/wiki/spec/"' in html
    assert 'href="/issue/ACME-1/"' in html
    assert 'href="/api/v1/attachments/1/download/"' in html


@pytest.mark.parametrize(
    "source",
    [
        "Inline `[x](spec.pdf)` and `![c](chart.png)` code.",
        "```\n[x](spec.pdf)\n![c](chart.png)\n```",
        "    [x](spec.pdf)\n    ![c](chart.png)",
    ],
    ids=["inline-code", "fenced-block", "indented-block"],
)
def test_filenames_in_code_stay_as_written(source):
    html = render(source)
    assert "/api/v1/attachments/" not in html
    assert "spec.pdf" in html
    assert "chart.png" in html


# Each builder makes an unterminated link target that grows with *k*.
_UNTERMINATED_TARGETS = {
    "words": lambda k: "[a](" + "x " * (5000 * k),
    "image-words": lambda k: "![a](" + "x " * (5000 * k),
    "spaces": lambda k: "[a](" + " " * (10000 * k),
    "unclosed-titles": lambda k: "[a](" + 'spec.pdf "' * (1250 * k),
    "unclosed-angle": lambda k: "[a](<" + "x " * (5000 * k),
    "many-lines": lambda k: ("[a](" + "x " * (50 * k) + "\n") * 100,
    "spaces-before-word": lambda k: "[a](x" + " " * (5000 * k) + "y",
}


@pytest.mark.parametrize("name", list(_UNTERMINATED_TARGETS))
def test_unterminated_targets_render_in_linear_time(name):
    """Regression: a backtracking target pattern made these take seconds or more.

    Measured as scaling (CPU time at 4x the size), not as absolute time, so a
    slow or busy machine cannot fail it.
    """
    build = _UNTERMINATED_TARGETS[name]
    assert scaling_ratio(render, build(1), build(4)) < LINEAR_RATIO_LIMIT
    assert cpu_time(lambda: render(build(16)), repeat=1) < CATASTROPHE_SECONDS


def test_many_unterminated_targets_in_one_paragraph_are_not_rendered():
    """Python-Markdown rescans the paragraph once per unclosed target; the render budget stops that."""
    assert FALLBACK in render(("[a](" + "x " * 200 + "\n") * 1000)
