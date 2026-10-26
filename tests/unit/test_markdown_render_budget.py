"""Unit tests for the Markdown render budget.

Inputs that Python-Markdown renders in super-linear time, or that exhaust the
recursion limit, are shown as escaped plain text instead. Realistic large
documents keep rendering exactly as before.

Nothing here asserts an absolute duration, which depends on the machine and
its load. The pre-scan's decision is asserted directly, linear-time claims
are asserted as scaling in CPU time (``scaling_ratio``), and the only
absolute bound (``CATASTROPHE_SECONDS``) is far above any expected time.
"""

from __future__ import annotations

import time

import markdown
import pytest

from specivo.core.config import get_settings
from specivo.core.exceptions import ValidationError
from specivo.core.i18n import activate, deactivate
from specivo.services import markdown_budget, markdown_service
from specivo.services.markdown_budget import (
    MAX_MARKDOWN_CHARS,
    MAX_NESTING_DEPTH,
    RenderDeadline,
    RenderDeadlineError,
    _backtick_scan_chars,
    check_markdown_length,
    estimate_render_cost,
    render_hazard,
)
from specivo.services.markdown_service import (
    _MD_EXTENSION_CONFIGS,
    _MD_EXTENSIONS,
    _sanitize_html,
    render_plain_markdown,
    render_wiki_markdown,
    substitute_links,
)
from specivo.services.search_service import _clean_snippet
from specivo.services.wiki_link_service import WikiLinkService
from specivo.services.wiki_utils import WIKI_LINK_RE
from tests.fixtures.markdown_inputs import (
    CATASTROPHE_SECONDS,
    LINEAR_RATIO_LIMIT,
    PATHOLOGICAL,
    RECURSION,
    cpu_time,
    large_page,
    links_list,
    links_paragraph,
    scaling_ratio,
)

pytestmark = pytest.mark.unit

FALLBACK = 'class="sp-markdown-fallback"'


def render(text: str) -> str:
    return str(render_wiki_markdown(text, project_key="ACME"))


def timed_render(text: str) -> tuple[str, float]:
    started = time.thread_time()
    html = render(text)
    return html, time.thread_time() - started


@pytest.fixture
def markdown_never_called(monkeypatch):
    """Fail the test if Python-Markdown is asked to render anything."""

    def refuse(*args, **kwargs):
        raise AssertionError("the guard let this text through to Python-Markdown")

    monkeypatch.setattr(markdown_service._md, "markdown", refuse)


def unguarded(text: str) -> str:
    """What the renderer produced before the budget existed (no wiki or issue-ref substitution)."""
    return _sanitize_html(markdown.markdown(text, extensions=_MD_EXTENSIONS, extension_configs=_MD_EXTENSION_CONFIGS))


# ---------------------------------------------------------------------------
# Pathological inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(PATHOLOGICAL))
def test_pre_scan_rejects_pathological_input(name):
    text = PATHOLOGICAL[name]
    assert render_hazard(substitute_links(text, "ACME"), len(text)) is not None


@pytest.mark.parametrize("name", list(PATHOLOGICAL))
def test_pathological_input_is_shown_as_plain_text_without_rendering(name, markdown_never_called):
    html, elapsed = timed_render(PATHOLOGICAL[name])
    assert FALLBACK in html
    assert elapsed < CATASTROPHE_SECONDS


@pytest.mark.parametrize("name", list(PATHOLOGICAL))
def test_guarded_render_of_pathological_input_scales_linearly(name):
    """The pre-scan and the fallback are linear: a quarter of the text takes about a quarter of the time."""
    text = PATHOLOGICAL[name]
    assert scaling_ratio(render, text[: len(text) // 4], text) < LINEAR_RATIO_LIMIT


def test_recursion_error_falls_back_to_plain_text():
    # Nested emphasis passes the pre-scan; the render itself hits the recursion limit.
    assert render_hazard(RECURSION) is None
    html, elapsed = timed_render(RECURSION)
    assert FALLBACK in html
    assert elapsed < CATASTROPHE_SECONDS


def test_plain_markdown_filter_is_guarded_too(markdown_never_called):
    assert FALLBACK in str(render_plain_markdown(PATHOLOGICAL["unclosed-link-target"]))


def test_fallback_escapes_the_text_and_keeps_it_whole():
    text = "<script>alert(1)</script> & " + "[a](x " * 20000
    html = str(render_wiki_markdown(text))
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; [a](x [a](x " in html
    assert html.count("[a](x") == 20000


def test_fallback_notice_is_translated():
    activate("th")
    try:
        html = str(render_wiki_markdown(PATHOLOGICAL["bare-brackets"]))
    finally:
        deactivate()
    assert "This text is too complex to format" not in html
    assert FALLBACK in html


def test_render_past_the_deadline_falls_back(monkeypatch):
    monkeypatch.setattr(get_settings(), "markdown_render_deadline_seconds", -1.0)
    assert FALLBACK in str(render_wiki_markdown("Just a *short* paragraph."))


def test_deadline_defaults_to_a_wide_margin():
    # The slowest realistic page needs well under a second of CPU time.
    assert get_settings().markdown_render_deadline_seconds >= 5.0


def test_deadline_counts_cpu_time_not_waiting():
    """Time the thread spends not running (a busy host, a sleep) does not count."""
    md = markdown.Markdown(extensions=[RenderDeadline(0.05)])
    time.sleep(0.2)
    assert md.convert("Still *renders*.") == "<p>Still <em>renders</em>.</p>"


def test_deadline_stops_a_render_that_uses_the_cpu():
    md = markdown.Markdown(extensions=[RenderDeadline(0.05)])
    started = time.thread_time()
    while time.thread_time() - started < 0.1:
        pass
    with pytest.raises(RenderDeadlineError):
        md.convert("Too *late*.")


# ---------------------------------------------------------------------------
# Realistic documents
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [links_list(), links_paragraph(), large_page(), large_page(MAX_MARKDOWN_CHARS - 1024)],
    ids=["2000-links-list", "2000-links-paragraph", "200-kb-page", "page-at-limit"],
)
def test_large_realistic_document_renders_unchanged(text):
    assert render_hazard(text) is None
    html = str(render_plain_markdown(text))
    assert FALLBACK not in html
    assert html == unguarded(text)


def test_two_thousand_links_all_render():
    html = str(render_wiki_markdown(links_list()))
    assert html.count('<a href="https://example.com/') == 2000
    assert html.count("<img ") == 2000


def test_large_page_with_wiki_links_and_issue_refs_renders():
    html = render(large_page())
    assert FALLBACK not in html
    assert '<a href="/projects/ACME/wiki/page-0/"' in html
    assert '<a href="/issue/ACME-0/"' in html


def test_realistic_rendering_scales_linearly():
    assert scaling_ratio(render, large_page(50 * 1024), large_page(200 * 1024)) < LINEAR_RATIO_LIMIT


def test_realistic_page_renders_far_inside_the_deadline():
    """The margin the deadline default relies on: a 250 KB page uses a fraction of it."""
    assert (
        cpu_time(lambda: render(large_page(250 * 1024)), repeat=1) < get_settings().markdown_render_deadline_seconds / 2
    )


def test_text_with_a_few_unclosed_openers_renders():
    text = "\n\n".join(f"Interval [0, {i}) and a stray `tick, a <b and a [link](x" for i in range(200))
    assert render_hazard(text) is None
    assert FALLBACK not in str(render_wiki_markdown(text))


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


def test_size_limit_applies_to_the_original_text():
    assert render_hazard("a" * MAX_MARKDOWN_CHARS) is None
    assert render_hazard("a" * (MAX_MARKDOWN_CHARS + 1)) == "size"
    # Substitution may lengthen the text; the author's length is what counts.
    assert render_hazard("a" * (MAX_MARKDOWN_CHARS + 1), original_length=MAX_MARKDOWN_CHARS) is None


@pytest.mark.parametrize(
    ("prefix", "depth"),
    [("> ", 1), ("- ", 1), ("1. ", 1), ("    ", 1), ("\t", 1), ("> - ", 2)],
)
def test_nesting_limit(prefix, depth):
    levels = MAX_NESTING_DEPTH // depth
    assert render_hazard(prefix * levels + "a") is None
    assert render_hazard(prefix * (levels + 1) + "a") == "nesting"


def test_cost_budget_boundary(monkeypatch):
    text = "[" * 1000
    cost = estimate_render_cost(text)
    monkeypatch.setattr(markdown_budget, "RENDER_BUDGET_US", cost)
    assert render_hazard(text) is None
    monkeypatch.setattr(markdown_budget, "RENDER_BUDGET_US", cost - 1)
    assert render_hazard(text) == "cost"


def test_unclosed_link_targets_are_flagged_and_closed_ones_are_not():
    assert render_hazard("[a](b) " * 2000) is None
    assert render_hazard("[a](b " * 2000) == "cost"


def test_unclosed_openers_only_rescan_their_own_paragraph():
    assert render_hazard("[" * 3000) == "cost"
    assert render_hazard(("[" * 30 + "\n\n") * 100) is None


def test_escaped_openers_are_not_rescanned():
    assert render_hazard("\\[" * 3000) is None
    assert render_hazard("[" * 3000) == "cost"


def test_backtick_runs_pair_by_length():
    # The run of two closes at the next run of two; the single tick inside is code.
    assert _backtick_scan_chars([(0, 2), (5, 1), (8, 2)], 20) == 8
    # Without a closer, every backtick of a run reads to the end of the block.
    assert _backtick_scan_chars([(0, 2), (5, 1)], 20) == 2 * 20 + 1 * 15


def test_duplicate_headings_threshold():
    assert render_hazard("## Example\n\ntext\n\n" * 100) is None
    assert render_hazard("## Example\n\ntext\n\n" * 3000) == "cost"
    # Distinct headings are fine in the same number.
    assert render_hazard("".join(f"## Example {i}\n\ntext\n\n" for i in range(3000))) is None


def test_toc_marker_cost_scales_with_headings():
    headings = "".join(f"## H{i}\n\n" for i in range(300))
    assert render_hazard(headings + "[TOC]\n\n") is None
    assert render_hazard(headings + "[TOC]\n\n" * 1000) == "cost"


# ---------------------------------------------------------------------------
# Stored-text limit
# ---------------------------------------------------------------------------


def test_check_markdown_length():
    check_markdown_length(None, "description")
    check_markdown_length("a" * MAX_MARKDOWN_CHARS, "description")
    with pytest.raises(ValidationError) as exc_info:
        check_markdown_length("a" * (MAX_MARKDOWN_CHARS + 1), "notes")
    assert exc_info.value.field == "notes"
    assert exc_info.value.details == {"max_length": MAX_MARKDOWN_CHARS}


# ---------------------------------------------------------------------------
# Wiki link regex
# ---------------------------------------------------------------------------


def _parse_wiki_links(text: str) -> None:
    WIKI_LINK_RE.sub("", text)
    WikiLinkService().parse_links(text)
    _clean_snippet(text)


@pytest.mark.parametrize("unit", ["[[", "[[a|", "[[a"])
def test_wiki_link_parsing_is_linear(unit):
    assert scaling_ratio(_parse_wiki_links, unit * 12500, unit * 50000) < LINEAR_RATIO_LIMIT


def test_wiki_link_regex_still_matches_links():
    found = WIKI_LINK_RE.findall("see [[Page One]], [[Page|label]] and [[A|b|c]]")
    assert found == [("Page One", ""), ("Page", "label"), ("A", "b|c")]
