"""Static source rules that are cheaper to check as a lint step than as tests.

Run by ``make lint`` and CI. Stdlib only, so it needs no test database.

Rules:
  * Specivo has one edition: no template feature gates, no edition wording in
    templates or the catalogue template.
  * A form holding a field uses ``.sp-btn``, not ``.sp-btn-sm``, so the control
    heights match.
  * The member autocomplete dropdown is not clipped by its card: the card
    overrides ``overflow`` and the suggest field and panel have a z-index.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "specivo"
TEMPLATES = PACKAGE / "templates"
THEME = TEMPLATES / "themes" / "default"
CSS = ROOT / "frontend" / "css"

#: Phrases that would tell a user a feature belongs to another edition.
EDITION_PHRASES = ("Pro or Enterprise", "Specivo Pro", "Specivo Enterprise")

_FORM = re.compile(r"<form\b.*?</form>", re.S)
# A visible field: a select, a textarea, or an input that is not hidden, a checkbox or a radio.
_FIELD = re.compile(r"<(?:select|textarea)\b|<input\b(?![^>]*type=\"(?:hidden|checkbox|radio)\")")


def check_no_feature_gates() -> list[str]:
    return [
        f"{path.relative_to(ROOT)}: template feature gate has_feature()"
        for path in sorted(TEMPLATES.rglob("*.html"))
        if "has_feature(" in path.read_text(encoding="utf-8")
    ]


def check_no_edition_wording() -> list[str]:
    sources = [*TEMPLATES.rglob("*.html"), *(PACKAGE / "locale").glob("*.pot")]
    if not sources:
        return ["no templates or catalogue template found"]
    return [
        f"{path.relative_to(ROOT)}: edition wording {phrase!r}"
        for path in sorted(sources)
        for phrase in EDITION_PHRASES
        if phrase in path.read_text(encoding="utf-8")
    ]


def check_no_small_buttons_next_to_fields() -> list[str]:
    found = []
    for path in sorted(THEME.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for match in _FORM.finditer(text):
            body = match.group(0)
            if "sp-btn-sm" in body and _FIELD.search(body):
                line = text.count("\n", 0, match.start()) + 1
                found.append(f"{path.relative_to(ROOT)}:{line}: .sp-btn-sm in a form with a field; use .sp-btn")
    return found


def _css_text() -> str:
    files = sorted(CSS.glob("*.css")) + sorted((CSS / "pages").glob("*.css"))
    return "\n".join(p.read_text(encoding="utf-8") for p in files)


def _rule_body(css: str, selector: str) -> str | None:
    pattern = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(selector) + r"\s*\{([^}]*)\}")
    match = pattern.search(css)
    return match.group(1) if match else None


def check_suggest_dropdown_is_not_clipped() -> list[str]:
    css = _css_text()
    problems = []
    for selector, needs in (
        (".sp-card-pad-mb-md", ("overflow: visible", "position: relative")),
        (".sp-suggest-field", ("position: relative",)),
        (".sp-suggest-panel", ("position: absolute",)),
    ):
        body = _rule_body(css, selector)
        if body is None:
            problems.append(f"frontend/css: rule {selector} not found")
            continue
        problems += [f"frontend/css: {selector} must declare {need!r}" for need in needs if need not in body]
        if not re.search(r"z-index:\s*\d+", body):
            problems.append(f"frontend/css: {selector} needs an explicit z-index")
    panel = _rule_body(css, ".sp-suggest-panel") or ""
    match = re.search(r"z-index:\s*(\d+)", panel)
    if match and int(match.group(1)) < 1000:
        problems.append("frontend/css: .sp-suggest-panel z-index must be >= 1000 to overlay later cards")
    return problems


CHECKS = (
    check_no_feature_gates,
    check_no_edition_wording,
    check_no_small_buttons_next_to_fields,
    check_suggest_dropdown_is_not_clipped,
)


def main() -> int:
    problems = [problem for check in CHECKS for problem in check()]
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
