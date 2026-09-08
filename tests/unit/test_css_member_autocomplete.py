"""CSS contract tests for the project members tab and the admin groups page.

Started as a regression test for the "Add Member" autocomplete and grew with
the tab: a membership is now held by a user *or* a user group, so the table
shows two kinds of row and the delete of a group has to state what it costs.

The dropdown (`.sp-suggest-panel`) lives inside a `.card.sp-card-pad-mb-md`
wrapper. The shared `.card` rule sets `overflow: hidden`, which clipped the
dropdown so it disappeared underneath the next "Project Members" card.

This test guards against regressions by asserting that:
  * `.sp-card-pad-mb-md` overrides `overflow` so the dropdown can extend
    outside the card's box.
  * `.sp-suggest-field` and `.sp-suggest-panel` get an explicit z-index high
    enough to overlay later sibling cards (which would otherwise paint on
    top in document order).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# The stylesheet is authored as component source files under frontend/css/ and
# bundled by esbuild into a content-hashed dist file at build time. Read the
# tracked source directly so this test does not depend on a frontend build.
CSS_DIR = Path(__file__).resolve().parents[2] / "frontend" / "css"


@pytest.fixture(scope="module")
def css_text() -> str:
    """Every authored partial, top-level ones first then the page-specific ones.

    ``_rule_body`` returns the first match, so the order matters: keeping the
    top-level partials ahead of ``pages/`` preserves which declaration a
    selector defined in both would resolve to.
    """
    files = sorted(CSS_DIR.glob("*.css")) + sorted((CSS_DIR / "pages").glob("*.css"))
    parts = [p.read_text(encoding="utf-8") for p in files]
    assert parts, f"no CSS source files found in {CSS_DIR}"
    return "\n".join(parts)


def _rule_body(css: str, selector: str) -> str:
    """Return the declaration block for an exact selector match."""
    pattern = re.compile(
        r"(?<![A-Za-z0-9_-])" + re.escape(selector) + r"\s*\{([^}]*)\}",
    )
    match = pattern.search(css)
    assert match is not None, f"selector {selector!r} not found in frontend/css source"
    return match.group(1)


def test_card_pad_mb_md_does_not_clip_dropdown(css_text: str) -> None:
    body = _rule_body(css_text, ".sp-card-pad-mb-md")
    assert "overflow: visible" in body, (
        ".sp-card-pad-mb-md must set overflow: visible so the member "
        "autocomplete dropdown is not clipped by the parent card "
        "(which inherits overflow: hidden from .card)."
    )
    assert "position: relative" in body
    assert re.search(r"z-index:\s*\d+", body), (
        ".sp-card-pad-mb-md needs an explicit z-index so its dropdown stacks above later sibling cards."
    )


def test_suggest_field_creates_stacking_context(css_text: str) -> None:
    body = _rule_body(css_text, ".sp-suggest-field")
    assert "position: relative" in body
    assert re.search(r"z-index:\s*\d+", body), (
        ".sp-suggest-field needs an explicit z-index to ensure the "
        "absolutely-positioned .sp-suggest-panel paints above sibling cards."
    )


def test_suggest_panel_overlays_sibling_cards(css_text: str) -> None:
    body = _rule_body(css_text, ".sp-suggest-panel")
    assert "position: absolute" in body
    match = re.search(r"z-index:\s*(\d+)", body)
    assert match is not None, ".sp-suggest-panel must declare a z-index"
    # Modal-scale tokens used elsewhere are 1040-1055; the dropdown should
    # sit at or above 1000 so it overlays normal page content reliably.
    assert int(match.group(1)) >= 1000, (
        f".sp-suggest-panel z-index is {match.group(1)}; expected >= 1000 so the dropdown overlays subsequent cards."
    )


# ---------------------------------------------------------------------------
# Group rows in the members table
#
# The tab now lists user rows and group rows together. Two kinds in one table
# only work if the kind is visible, and the group row's expansion — the users
# a group covers — must not be what makes the page scroll sideways.
# ---------------------------------------------------------------------------


def test_principal_kind_marker_distinguishes_the_two_kinds(css_text: str) -> None:
    """A group row and a user row are addressed by different URLs; say which is which."""
    _rule_body(css_text, ".sp-principal-kind")

    group = _rule_body(css_text, ".sp-principal-kind-group")
    user = _rule_body(css_text, ".sp-principal-kind-user")
    assert group != user, (
        "the group and user markers must not resolve to the same styling, "
        "or the table shows two kinds of row with no way to tell them apart."
    )


def test_group_people_list_is_indented_under_its_row(css_text: str) -> None:
    body = _rule_body(css_text, ".sp-group-people")
    assert "list-style: none" in body
    assert "border-left" in body, (
        ".sp-group-people needs a visible left edge so the people it names "
        "read as belonging to the group row above them, not as rows of their own."
    )


def test_wide_member_table_scrolls_inside_its_own_container(css_text: str) -> None:
    """ADR-0005: the page must not scroll horizontally; the table does."""
    body = _rule_body(css_text, ".sp-table-scroll")
    assert "overflow-x: auto" in body


def test_delete_blast_radius_is_styled_as_a_warning(css_text: str) -> None:
    """Deleting a group revokes access in bulk; the counts must not read as a footnote."""
    body = _rule_body(css_text, ".sp-blast-radius")
    assert "--sp-danger" in body

    number = _rule_body(css_text, ".sp-blast-number")
    assert "--sp-font-size-xl" in number, (
        ".sp-blast-number must be typographically prominent — it is the number "
        "an administrator has to read before confirming the delete."
    )
