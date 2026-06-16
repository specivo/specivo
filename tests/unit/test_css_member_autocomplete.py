"""CSS regression test for the project members "Add Member" autocomplete.

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

CSS_PATH = (
    Path(__file__).resolve().parents[2]
    / "specivo"
    / "static"
    / "css"
    / "specivo.css"
)


@pytest.fixture(scope="module")
def css_text() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def _rule_body(css: str, selector: str) -> str:
    """Return the declaration block for an exact selector match."""
    pattern = re.compile(
        r"(?<![A-Za-z0-9_-])"
        + re.escape(selector)
        + r"\s*\{([^}]*)\}",
    )
    match = pattern.search(css)
    assert match is not None, f"selector {selector!r} not found in specivo.css"
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
        ".sp-card-pad-mb-md needs an explicit z-index so its dropdown "
        "stacks above later sibling cards."
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
        f".sp-suggest-panel z-index is {match.group(1)}; expected >= 1000 so "
        f"the dropdown overlays subsequent cards."
    )
