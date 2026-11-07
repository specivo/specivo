"""Form controls share one height: the tokens exist, and no template puts a small button next to a field.

The rendered heights are checked in the browser (``tests/e2e/test_form_control_sizing.py``);
these are the static rules that keep new templates from drifting.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "specivo" / "templates" / "themes" / "default"
CSS = ROOT / "frontend" / "css"

_FORM = re.compile(r"<form\b.*?</form>", re.S)
# A visible field: a select, a textarea, or an input that is not hidden, a checkbox or a radio.
_FIELD = re.compile(r"<(?:select|textarea)\b|<input\b(?![^>]*type=\"(?:hidden|checkbox|radio)\")")


def _forms_with_small_buttons_next_to_fields() -> list[str]:
    found = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for match in _FORM.finditer(text):
            body = match.group(0)
            if "sp-btn-sm" in body and _FIELD.search(body):
                line = text.count("\n", 0, match.start()) + 1
                found.append(f"{path.relative_to(TEMPLATES)}:{line}")
    return found


def test_no_form_mixes_small_buttons_with_fields():
    """.sp-btn-sm is for table rows and toolbars; a form with a field uses .sp-btn so the heights match."""
    assert _forms_with_small_buttons_next_to_fields() == []


def test_scanner_finds_a_small_button_next_to_a_field():
    sample = '<form><select name="x"></select><button class="sp-btn-sm">Go</button></form>'
    assert _FORM.search(sample) and _FIELD.search(sample)
    hidden_only = '<form><input type="hidden" name="x"><button class="sp-btn-sm">Go</button></form>'
    assert not _FIELD.search(hidden_only)


def test_controls_share_the_height_token():
    tokens = (CSS / "tokens.css").read_text(encoding="utf-8")
    assert "--sp-control-height:" in tokens and "--sp-control-height-sm:" in tokens
    for path, selector in [
        ("buttons.css", ".sp-btn {"),
        ("pages/new-issue.css", ".form-input {"),
        ("tables.css", ".filter-select {"),
        ("tables.css", ".filter-input {"),
    ]:
        text = (CSS / path).read_text(encoding="utf-8")
        start = text.index(selector)
        rule = text[start : text.index("}", start)]
        assert "min-height: var(--sp-control-height)" in rule, selector


def test_select_arrow_is_inset_by_the_base_rule():
    forms = (CSS / "forms.css").read_text(encoding="utf-8")
    assert "padding-right: var(--sp-select-padding-right)" in forms
    assert "background-position: right var(--sp-select-arrow-inset) center" in forms
    assert '@import "./forms.css";' in (CSS / "specivo.css").read_text(encoding="utf-8")
