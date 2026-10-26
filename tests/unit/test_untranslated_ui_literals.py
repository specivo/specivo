"""Guards against UI text that gettext and the viewer's locale never reach.

Three ways English slipped past the catalogues before, each scanned for here:

- string literals written into Alpine expressions in templates
  (``x-text="saving ? 'Saving...' : 'Save'"``), which the extractor never sees;
- ``{% trans %}`` / ``_()`` output spliced into such a literal
  (``x-text="ok ? '{% trans %}Copied!{% endtrans %}' : ..."``), which is
  translated but breaks the expression as soon as a translation contains an
  apostrophe (French does, often);
- dates formatted with a hardcoded English locale: ``'en-US'``, English
  month-name arrays, or strftime month names (``%b`` / ``%B``), which are
  English whatever the language.

Translated text reaches a component through its ``i18n``/``labels`` object or
``data-msg-*`` attributes; dates go through ``spFormatDate`` (browser) or the
Babel-backed ``localdt`` / ``localdate`` filters (server).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import specivo

pytestmark = pytest.mark.unit

_PACKAGE = Path(specivo.__file__).resolve().parent
_TEMPLATES = _PACKAGE / "templates"
_FRONTEND_JS = _PACKAGE.parent / "frontend" / "js"

# Alpine directives and bindings: x-text, x-show, :placeholder, @click, ...
_ALPINE_ATTR = re.compile(r"""\s((?:x-[\w:.-]+|:[\w.-]+|@[\w.-]+))\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.S)
# Attributes whose literals are class names or style, never visible text.
_NON_TEXT_ATTRS = {":class", "x-bind:class", ":style", "x-bind:style", "x-transition"}
# A JavaScript string literal inside an expression.
_JS_LITERAL = re.compile(r"'([^'\\]*)'|`([^`]*)`|\"([^\"\\]*)\"")
# Reads as prose: a capitalised word, two words, a word with leading or
# trailing space (a concatenation fragment), an ellipsis or an exclamation.
_PROSE = re.compile(
    r"(?:^|[^\w-])[A-Z][a-z]+|[A-Za-z]{2,}\s+[A-Za-z]{2,}|^\s+[A-Za-z]{2,}|[A-Za-z]{2,}\s+$|^[a-z]+\.{3}$|[A-Za-z]!$"
)

# Jinja is replaced before scanning: translation output by _TRANSLATED, any
# other expression or tag by _DATA, keeping line numbers intact.
_TRANSLATED = "\x01"
_DATA = "\x00"
_TRANS_BLOCK = re.compile(r"\{%-?\s*trans\b.*?\{%-?\s*endtrans\s*-?%\}", re.S)
_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.S)
_GETTEXT_CALL = re.compile(r"\b_\(|\bgettext\(|\bngettext\(")


def _blank(marker: str):
    return lambda m: marker + "\n" * m.group(0).count("\n")


def _strip_jinja(source: str) -> str:
    source = _TRANS_BLOCK.sub(_blank(_TRANSLATED), source)
    return _JINJA.sub(
        lambda m: (_TRANSLATED if _GETTEXT_CALL.search(m.group(0)) else _DATA) + "\n" * m.group(0).count("\n"),
        source,
    )


def scan_template(source: str) -> list[tuple[int, str, str]]:
    """Return ``(line, attribute, literal)`` for each untranslatable literal in *source*."""
    text = _strip_jinja(source)
    found: list[tuple[int, str, str]] = []
    for attr in _ALPINE_ATTR.finditer(text):
        name = attr.group(1)
        if name in _NON_TEXT_ATTRS:
            continue
        expr = attr.group(2) if attr.group(2) is not None else attr.group(3)
        for lit in _JS_LITERAL.finditer(expr):
            value = next((g for g in lit.groups() if g is not None), "")
            if not value:
                continue
            spliced = _TRANSLATED in value
            english = _DATA not in value and _PROSE.search(value)
            if spliced or english:
                line = text.count("\n", 0, attr.start()) + 1
                found.append((line, name, value.replace(_TRANSLATED, "{% trans %}")))
    return found


def _templates() -> list[Path]:
    return sorted(_TEMPLATES.rglob("*.html"))


# ---------------------------------------------------------------------------
# The scanner itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "snippet",
    [
        """<span x-text="saving ? 'Saving...' : 'Save'"></span>""",
        """<span x-text="v.open_count + ' open'"></span>""",
        """<input :placeholder="tags.length ? 'Add more...' : ''">""",
        """<b x-text="ok ? '{% trans %}Copied!{% endtrans %}' : ''"></b>""",
        """<b :title="on ? '{{ _('Disable') }}' : ''"></b>""",
    ],
)
def test_scanner_flags_untranslatable_literals(snippet: str):
    assert scan_template(snippet), f"the scanner missed {snippet!r}"


@pytest.mark.parametrize(
    "snippet",
    [
        """<span x-text="saving ? i18n.saving : i18n.save"></span>""",
        """<div x-show="tab === 'general'" :class="{ 'sp-active': on }"></div>""",
        """<a :href="'/projects/' + key + '/'" x-text="'(' + count + ')'"></a>""",
        """<div x-data='comp({ i18n: {{ {"save": _("Save")} | tojson }} })'></div>""",
        """<div x-data="comp({ id: '{{ issue.display_key }}' })"></div>""",
    ],
)
def test_scanner_accepts_translated_or_non_text_literals(snippet: str):
    assert scan_template(snippet) == []


# ---------------------------------------------------------------------------
# The codebase
# ---------------------------------------------------------------------------


def test_no_untranslatable_literals_in_alpine_expressions():
    offenders = [
        f"{path.relative_to(_TEMPLATES)}:{line}: {attr}: {literal!r}"
        for path in _templates()
        for line, attr, literal in scan_template(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        "Pass these strings into the component (i18n/labels object or data-msg-*) "
        "instead of writing them into the expression:\n" + "\n".join(offenders)
    )


_JS_HARDCODED_DATE = re.compile(r"""['"]en-[A-Z]{2}['"]|toLocale(?:Date|Time)?String\(|['"]Jan['"]\s*,\s*['"]Feb['"]""")


def test_frontend_dates_follow_the_page_locale():
    """Dates in components go through spFormatDate, never a fixed or browser locale."""
    sources = sorted(_FRONTEND_JS.rglob("*.js"))
    if not sources:
        pytest.skip("frontend sources are not part of this installation")
    offenders = [
        f"{path.relative_to(_FRONTEND_JS)}:{n}: {line.strip()}"
        for path in sources
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _JS_HARDCODED_DATE.search(line)
    ]
    assert offenders == [], "Use spFormatDate() (frontend/js/lib/globals.js):\n" + "\n".join(offenders)


def test_templates_have_no_hardcoded_en_us():
    offenders = [str(p.relative_to(_TEMPLATES)) for p in _templates() if re.search(r"en-US", p.read_text("utf-8"))]
    assert offenders == []


_ENGLISH_STRFTIME = re.compile(r"strftime\(\s*['\"][^'\"]*%[bBaA]")


def test_server_dates_do_not_use_english_strftime_names():
    """``%b``/``%B``/``%a``/``%A`` are English month and weekday names; use Babel instead."""
    files = [*_templates(), *sorted(_PACKAGE.rglob("*.py"))]
    offenders = [
        f"{path.relative_to(_PACKAGE)}:{n}"
        for path in files
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _ENGLISH_STRFTIME.search(line)
    ]
    assert offenders == [], "Use the localdt/localdate filters or specivo.core.i18n.format_date:\n" + "\n".join(
        offenders
    )
