"""Static source rules that are cheaper to check as a lint step than as tests.

Run by ``make lint`` and CI. Stdlib only, so it needs no test database.

Rules:
  * Specivo has one edition: no template feature gates, no edition wording in
    templates or the catalogue template.
  * A form holding a field uses ``.sp-btn``, not ``.sp-btn-sm``, so the control
    heights match.
  * The member autocomplete dropdown is not clipped by its card: the card
    overrides ``overflow`` and the suggest field and panel have a z-index.
  * Every URL the UI writes carries the stealth prefix (``STEALTH_PREFIX``):
    a root-relative URL in a template starts with ``{{ base }}`` (or
    ``base ~`` in a Jinja expression); a URL attribute filled from a variable
    goes through the ``prefixed`` filter or is allow-listed below with the
    reason it already carries the prefix; a root-relative string literal in
    ``frontend/js`` or the service worker is the argument of ``spFetch(...)``
    or ``spUrl(...)``; a stylesheet ``url()`` is relative to the bundle.
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


# ---------------------------------------------------------------------------
# Stealth prefix: no root-relative URL leaves the prefix
# ---------------------------------------------------------------------------

JS = ROOT / "frontend" / "js"
SERVICE_WORKER = PACKAGE / "static" / "sw.js"

# A single slash followed by a path character: "/projects/", not "//cdn" or "/".
_ROOT_REL = r"/(?![/\\\s'\"`>])"
_JINJA_TOKEN = re.compile(r"(\{#.*?#\}|\{\{.*?\}\}|\{%.*?%\})", re.S)
# In template text: a quoted value that starts with a root-relative path.
_TEMPLATE_LITERAL = re.compile(r"[\"'`](?=" + _ROOT_REL + ")")
# In a Jinja expression: a string literal that starts with one.
_JINJA_LITERAL = re.compile(r"[\"'](?=" + _ROOT_REL + ")")
# A URL attribute whose value starts with a Jinja expression.
_URL_ATTR_EXPR = re.compile(
    r"\s(?:href|src|action|formaction|poster|hx-(?:get|post|put|patch|delete)|data-[\w-]*(?:url|src|href|redirect))"
    r"=[\"']\{\{-?\s*(.*?)\s*-?\}\}",
    re.S,
)
# The bare site root, written where a URL goes: href="/", "url": "/", x or "/".
_BARE_ROOT_ATTR = re.compile(r"\s(?:href|src|action|formaction|hx-(?:get|post)|data-[\w-]*url)=([\"'])/\1")
_BARE_ROOT_EXPR = re.compile(r"(?:[\"']url[\"']\s*:|\bor)\s*[\"']/[\"']")
_CSS_ROOT_URL = re.compile(r"url\(\s*['\"]?" + _ROOT_REL)
# In JS: a string literal that starts with a root-relative path.
_JS_LITERAL = re.compile(r"(?<![\w\\])[\"'`](?=" + _ROOT_REL + r"[\w{$])")
_JS_COMMENT = re.compile(r"/\*.*?\*/|(?<![:'\"\\])//[^\n]*", re.S)

#: URL attribute expressions that hold an absolute URL, a query-only URL or a
#: path the template itself built with ``base ~``, with the reason. Keys are
#: ``"<template under specivo/templates>:<expression>"``, or a bare expression
#: for one allowed everywhere. Anything else written into href/src/action/hx-*
#: from a variable must use ``| prefixed`` (idempotent) or ``{{ base }}{{ path }}``.
PREFIXED_URL_EXPRESSIONS: dict[str, str] = {
    "base": "the prefix itself, as in {{ base }}{{ path }}",
    "sign_in_url()": "login_url_for() prefixes the login page; next is the request path",
    "_shared/email/account_notice.html:action_url": "absolute: public_url() (APP_URL + prefix)",
    "_shared/email/issue_event.html:url": "absolute: public_url() (APP_URL + prefix)",
    "_shared/email/password_reset.html:reset_url": "absolute: public_url() (APP_URL + prefix)",
    "themes/default/base.html:canonical_url": "absolute: APP_URL + the request path",
    "themes/default/components/app_url_warning.html:notice.guide_url": "absolute: the online guide",
    "themes/default/pages/ai_usage/_issue_tab.html:guide_url": "absolute: the online guide",
    "themes/default/pages/issues/_development_items.html:extra.branch_url": "absolute: the forge's branch page",
    "themes/default/components/macros.html:href": "breadcrumbs(): callers pass base ~ '/...' URLs",
    "themes/default/components/macros.html:link": "tag_badge(): callers pass base ~ '/search/...'",
    "themes/default/components/macros.html:base_url": "pagination(): callers pass base ~ '/...' or x | prefixed",
    "themes/default/pages/auth/login.html:local_login_url": "query-only URL (?next=...)",
    "themes/default/pages/auth/login.html:next_path or base ~ '/'": "safe_next_path(): only paths under the prefix",
    "themes/default/pages/issues/_development_items.html:timeline_url": "set above with base ~",
    "themes/default/pages/issues/_development_timeline.html:timeline_url": "set above with base ~",
    "themes/default/pages/search.html:pbase": "set above with base ~",
}

# An Alpine URL binding: :href="...", :src="..." (and x-bind:).
_ALPINE_URL_BINDING = re.compile(r"\s(?:x-bind)?:(?:href|src|action)=\"([^\"]*)\"")

#: Alpine URL binding expressions that already give a prefixed or non-local URL,
#: with the reason. Any other binding starts with spUrl( or '{{ base }}/.
ALPINE_URL_BINDINGS: dict[str, str] = {
    "tagSearchUrl(tag.name)": "tag-field.js: returns spUrl(...)",
    "groupUrl(g)": "admin-groups.js: returns spUrl(...)",
    "lightbox.url": "attachments.js: spUrl(downloadPath(att))",
    "val.href": "issue-metadata-panel.js: metadataSearchUrl() returns spUrl(...)",
    "r.repo_url": "absolute: the repository on the forge",
}

#: Template macros that build a query-only URL ("?a=b"), allowed as the start
#: of a URL attribute expression in any template.
QUERY_URL_MACROS = ("col_page_url(",)

#: Root-relative JS string literals that are deliberately not prefixed, with the reason.
JS_LITERAL_EXCEPTIONS: dict[tuple[str, str], str] = {
    ("frontend/js/components/attachments.js", "'/api/v1/attachments/'"): (
        "downloadPath(): the application path a Markdown link holds; the renderer adds the prefix, "
        "navigation wraps it in spUrl()"
    ),
    ("frontend/js/components/wiki-form.js", "'/api/v1/attachments/'"): (
        "Markdown source inserted into the editor; the renderer adds the prefix"
    ),
    ("frontend/js/modules/wiki-history.js", "'/history/'"): "path segment swapped in location.pathname, not a URL",
    ("frontend/js/modules/wiki-history.js", "'/diff/'"): "path segment swapped in location.pathname, not a URL",
}


def _blank(match: re.Match[str]) -> str:
    """Replace a comment by as many newlines, so line numbers still match."""
    return "\n" * match.group(0).count("\n")


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _attr_expression_ok(template: str, expression: str) -> bool:
    expression = " ".join(expression.split())
    if expression in PREFIXED_URL_EXPRESSIONS or f"{template}:{expression}" in PREFIXED_URL_EXPRESSIONS:
        return True
    if expression.startswith(QUERY_URL_MACROS):
        return True
    return bool(re.search(r"\|\s*prefixed\b", expression))


def template_url_problems(name: str, raw: str) -> list[str]:
    """The prefix problems of template *raw*; *name* is its path under ``specivo/templates``."""
    problems = []
    text = re.sub(r"\{#.*?#\}", _blank, raw, flags=re.S)
    where = f"specivo/templates/{name}"
    offset = 0
    for i, part in enumerate(_JINJA_TOKEN.split(text)):
        if i % 2 == 0:
            for m in _TEMPLATE_LITERAL.finditer(part):
                before = part[max(0, m.start() - 40) : m.start()]
                if before.rstrip().endswith(("spFetch(", "spUrl(", "+")):
                    continue
                line = _line(text, offset + m.start())
                problems.append(f"{where}:{line}: root-relative URL without {{{{ base }}}}")
            problems.extend(
                f"{where}:{_line(text, offset + m.start())}: url() without {{{{ base }}}}"
                for m in _CSS_ROOT_URL.finditer(part)
            )
            problems.extend(
                f"{where}:{_line(text, offset + m.start())}: site root without {{{{ base }}}}"
                for m in _BARE_ROOT_ATTR.finditer(part)
            )
        elif part.startswith(("{{", "{%")):
            for m in _JINJA_LITERAL.finditer(part):
                if part[: m.start()].rstrip().endswith("~"):
                    continue
                line = _line(text, offset + m.start())
                problems.append(f"{where}:{line}: root-relative string in a Jinja expression without base ~")
            for m in _BARE_ROOT_EXPR.finditer(part):
                line = _line(text, offset + m.start())
                problems.append(f"{where}:{line}: site root in a Jinja expression without base ~")
        offset += len(part)
    for m in _ALPINE_URL_BINDING.finditer(text):
        value = m.group(1).strip()
        if value.startswith(("spUrl(", "'{{ base }}/", "'#'")) or value in ALPINE_URL_BINDINGS:
            continue
        problems.append(
            f"{where}:{_line(text, m.start())}: Alpine URL binding {value!r}: wrap it in spUrl(...) "
            "or allow-list it in ALPINE_URL_BINDINGS"
        )
    for m in _URL_ATTR_EXPR.finditer(text):
        if not _attr_expression_ok(name, m.group(1)):
            line = _line(text, m.start())
            problems.append(
                f"{where}:{line}: URL attribute from {{{{ {m.group(1)} }}}}: use | prefixed, "
                "{{ base }}{{ ... }}, or allow-list it in PREFIXED_URL_EXPRESSIONS"
            )
    return problems


def check_template_urls_carry_the_prefix() -> list[str]:
    return [
        problem
        for path in sorted(TEMPLATES.rglob("*.html"))
        for problem in template_url_problems(path.relative_to(TEMPLATES).as_posix(), path.read_text(encoding="utf-8"))
    ]


def js_url_problems(where: str, raw: str) -> list[str]:
    """The prefix problems of JS source *raw*; *where* is its path from the repository root."""
    problems = []
    text = _JS_COMMENT.sub(_blank, raw)
    for m in _JS_LITERAL.finditer(text):
        before = text[max(0, m.start() - 40) : m.start()]
        if before.rstrip().endswith(("spFetch(", "spUrl(", "+")):
            continue
        end = text.find(text[m.start()], m.start() + 1)
        literal = text[m.start() : end + 1]
        if (where, literal) in JS_LITERAL_EXCEPTIONS:
            continue
        problems.append(f"{where}:{_line(text, m.start())}: {literal} outside spFetch()/spUrl()")
    return problems


def check_js_urls_carry_the_prefix() -> list[str]:
    return [
        problem
        for path in [*sorted(JS.rglob("*.js")), SERVICE_WORKER]
        for problem in js_url_problems(path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8"))
    ]


def check_css_urls_are_relative() -> list[str]:
    problems = []
    for path in sorted(CSS.rglob("*.css")):
        text = path.read_text(encoding="utf-8")
        problems.extend(
            f"{path.relative_to(ROOT)}:{_line(text, m.start())}: root-relative url(); "
            "write it relative to the bundle (../../fonts/...)"
            for m in _CSS_ROOT_URL.finditer(text)
        )
    return problems


CHECKS = (
    check_no_feature_gates,
    check_no_edition_wording,
    check_no_small_buttons_next_to_fields,
    check_suggest_dropdown_is_not_clipped,
    check_template_urls_carry_the_prefix,
    check_js_urls_carry_the_prefix,
    check_css_urls_are_relative,
)


def main() -> int:
    problems = [problem for check in CHECKS for problem in check()]
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
