"""Server-side markdown renderer — single source of truth for HTML output.

Both the Jinja ``markdown`` / ``wiki_markdown`` filters (used during template
rendering) and the ``POST /api/v1/markdown/preview`` endpoint (used by the
editor live preview) call into this module. Keeping a single implementation
prevents drift between saved content and editor preview — the entire point
of doing preview server-side rather than shipping a second renderer to the
browser.

Pipeline:

    raw markdown
      -> render_hazard(...)        over the size, nesting or cost budget:
                                   the raw text, escaped, with a notice instead
      -> markdown.markdown(...)    with fenced_code, tables, toc, codehilite
           + wiki link inline pattern: [[Page]] -> <a href="/projects/X/wiki/page/">
           + issue ref inline pattern: PROJ-123 -> <a href="/issue/PROJ-123/">
             (both on parsed text only, so code and raw HTML blocks are never
             touched; issue refs stay out of link text too)
           + attachment treeprocessor: <img src="b.png"> / <a href="b.pdf">
             -> attachment download URL (parsed tree, so code is never touched)
      -> nh3.clean(...)            HTML sanitizer with our allowlist
      -> markupsafe.Markup

``markdown.markdown`` also runs under a cooperative deadline, and a render
that overruns it or exhausts the recursion limit falls back to plain text too
(see ``specivo.services.markdown_budget``).
"""

from __future__ import annotations

import logging
import re
from urllib.parse import unquote
from xml.etree.ElementTree import Element

import markdown as _md
import markupsafe
import nh3
from markdown.extensions import Extension  # type: ignore[import-untyped]
from markdown.inlinepatterns import InlineProcessor  # type: ignore[import-untyped]
from markdown.treeprocessors import Treeprocessor  # type: ignore[import-untyped]

from specivo.core.i18n import gettext as _
from specivo.services import markdown_budget
from specivo.services.markdown_budget import (
    RenderDeadline,
    RenderDeadlineError,
    charge_render,
    render_allowance,
    render_hazard,
)
from specivo.services.wiki_utils import WIKI_LINK_RE as _WIKI_LINK_RE
from specivo.services.wiki_utils import slugify as _wiki_slugify

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sanitizer configuration — must match what was historically inline in
# web/deps.py. Any change here changes saved-content rendering everywhere.
# ---------------------------------------------------------------------------

_ALLOWED_TAGS: set[str] = {
    "p",
    "br",
    "hr",
    "a",
    "strong",
    "em",
    "b",
    "i",
    "u",
    "s",
    "del",
    "code",
    "pre",
    "blockquote",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "li",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "th",
    "td",
    "img",
    "div",
    "span",
    "mark",
    "sup",
    "sub",
    "dl",
    "dt",
    "dd",
}

_ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href", "title", "id"},
    "img": {"src", "alt", "title", "width", "height"},
    "th": {"align", "colspan", "rowspan"},
    "td": {"align", "colspan", "rowspan"},
    "code": {"class"},
    "pre": {"class"},
    "div": {"class"},
    "span": {"class"},
    "h1": {"id"},
    "h2": {"id"},
    "h3": {"id"},
    "h4": {"id"},
    "h5": {"id"},
    "h6": {"id"},
}

_MD_EXTENSIONS = ["fenced_code", "tables", "toc", "codehilite"]
_MD_EXTENSION_CONFIGS = {
    "codehilite": {"css_class": "codehilite", "guess_lang": False},
}

# Compiled once at import time
# A URL scheme (``https:``, ``mailto:``) at the start of a link target.
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
# Issue reference: PROJ-123, but not right after ``[``, ``(`` or ``/`` (a link
# label, a link target, a URL path).
_ISSUE_REF_PATTERN = r"(?<!\[)(?<!\()(?<!/)\b([A-Z][A-Z0-9]+-\d+)\b"
_ISSUE_REF_RE = re.compile(_ISSUE_REF_PATTERN)
# What Python-Markdown leaves in text for a backslash escape it has read.
_ESCAPED_CHAR_RE = re.compile("\x02(\\d+)\x03")


def count_issue_ref_candidates(text: str) -> int:
    """Return how many ``KEY-123`` tokens *text* holds, for the render budget.

    Counts every token, in code too, so the estimate never falls short of
    what the issue-ref inline pattern may link.
    """
    return sum(1 for _ in _ISSUE_REF_RE.finditer(text))


def find_issue_ref_candidates(text: str | None) -> set[str]:
    """Return all ``KEY-123`` issue-reference candidates appearing in *text*.

    Uses the same pattern as the autolinker, so callers can batch-resolve which
    candidates actually exist (or previously existed) before rendering — see
    ``IssueService.resolve_known_issue_refs``.
    """
    if not text:
        return set()
    return {m.group(1) for m in _ISSUE_REF_RE.finditer(text)}


def _sanitize_html(html_str: str) -> str:
    return nh3.clean(
        html_str,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        link_rel="noopener noreferrer",
        url_schemes={"http", "https", "mailto"},
    )


def _render_markdown(source: str, extensions: list, issue_refs: int = 0) -> markupsafe.Markup:
    """Render *source* to sanitized HTML, or as plain text if that is unsafe.

    *issue_refs* is how many ``KEY-123`` tokens the render may link (see
    :func:`render_hazard`).
    """
    hazard = render_hazard(source, issue_refs)
    if hazard is None:
        allowance = render_allowance()
        if allowance <= 0:
            hazard = "request budget"
    if hazard is None:
        started = markdown_budget._cpu_clock()
        try:
            html = _md.markdown(
                source,
                extensions=[*extensions, RenderDeadline(allowance)],
                extension_configs=_MD_EXTENSION_CONFIGS,
            )
        except RenderDeadlineError:
            hazard = "deadline"
        except RecursionError:
            hazard = "recursion"
        else:
            return markupsafe.Markup(_sanitize_html(html))
        finally:
            charge_render(markdown_budget._cpu_clock() - started)
    logger.warning("Markdown shown as plain text (%s, %d characters)", hazard, len(source))
    return _plain_text_fallback(source)


def _plain_text_fallback(text: str) -> markupsafe.Markup:
    """Show *text* escaped and preformatted, below a notice saying why."""
    notice = _("This text is too complex to format, so it is shown as plain text.")
    return markupsafe.Markup(
        '<div class="sp-markdown-fallback"><p class="sp-markdown-fallback-notice">{}</p><pre>{}</pre></div>'
    ).format(notice, text)


def _attachment_filename(target: str) -> str | None:
    """Return the attachment filename a link or image *target* names, or None.

    The target is URL-decoded first, so ``my%20spec.pdf`` names
    ``my spec.pdf``. Only a bare filename qualifies: anything with a ``/`` or
    ``\\`` (a path or URL), a scheme (``https:``, ``mailto:``) or starting
    with ``#`` or ``?`` is left to the browser.
    """
    name = unquote(target).strip()
    if not name or name[0] in "#?" or "/" in name or "\\" in name or _URL_SCHEME_RE.match(name):
        return None
    return name


class _AttachmentRefTreeprocessor(Treeprocessor):
    """Point bare-filename ``<img src>`` and ``<a href>`` at their attachment's download URL.

    Runs on the parsed tree, after inline patterns, so Markdown itself has
    already handled ``<my spec.pdf>`` targets, titles and escapes, and code
    spans and blocks (text, or stashed HTML) are never rewritten. Matching is
    exact and case-sensitive; unknown names stay as written.
    """

    def __init__(self, md: _md.Markdown, attachment_map: dict) -> None:
        super().__init__(md)
        self.attachment_map = attachment_map

    def run(self, root: Element) -> None:
        for element in root.iter():
            attribute = {"img": "src", "a": "href"}.get(element.tag)
            if attribute is None:
                continue
            target = element.get(attribute)
            if not target:
                continue
            filename = _attachment_filename(target)
            if filename is not None and filename in self.attachment_map:
                element.set(attribute, self.attachment_map[filename])


class _AttachmentRefExtension(Extension):
    def __init__(self, attachment_map: dict) -> None:
        super().__init__()
        self.attachment_map = attachment_map

    def extendMarkdown(self, md: _md.Markdown) -> None:  # noqa: N802 - Python-Markdown API
        # Below "inline" (20), so links and images exist when it runs.
        md.treeprocessors.register(_AttachmentRefTreeprocessor(md, self.attachment_map), "attachment_refs", 5)


class _IssueRefInlineProcessor(InlineProcessor):
    """Link ``KEY-123`` in parsed text to ``/issue/KEY-123/``.

    An inline pattern only sees text Python-Markdown parses as Markdown, so
    code spans, fenced and indented code blocks and raw HTML blocks (stashed
    before inline parsing) keep their keys as written, and ``ANCESTOR_EXCLUDES``
    keeps it out of link text. A key in inline HTML (``<span>ACME-1</span>``) is
    still linked, as an ``<a>`` element.
    """

    ANCESTOR_EXCLUDES = ("a",)

    def __init__(self, md: _md.Markdown, known_issue_refs: set[str] | None) -> None:
        super().__init__(_ISSUE_REF_PATTERN, md)
        self.known_issue_refs = known_issue_refs

    def handleMatch(  # noqa: N802 - Python-Markdown API
        self, m: re.Match[str], data: str
    ) -> tuple[Element | None, int | None, int | None]:
        ref = m.group(1)
        if self.known_issue_refs is not None and ref not in self.known_issue_refs:
            return None, None, None
        element = Element("a", {"href": f"/issue/{ref}/"})
        element.text = ref
        return element, m.start(0), m.end(0)


class _IssueRefExtension(Extension):
    def __init__(self, known_issue_refs: set[str] | None) -> None:
        super().__init__()
        self.known_issue_refs = known_issue_refs

    def extendMarkdown(self, md: _md.Markdown) -> None:  # noqa: N802 - Python-Markdown API
        # Below every built-in pattern (the lowest is emphasis at 50), so links,
        # autolinks, inline HTML and emphasis are matched first and a key in
        # link text is seen with an ``a`` ancestor.
        md.inlinePatterns.register(_IssueRefInlineProcessor(md, self.known_issue_refs), "issue_refs", 5)


class _WikiLinkInlineProcessor(InlineProcessor):
    """Link ``[[Page]]`` and ``[[Page|label]]`` in parsed text to ``/projects/KEY/wiki/<slug>/``.

    An inline pattern only sees text Python-Markdown parses as Markdown, so
    code spans, fenced and indented code blocks and raw HTML blocks (stashed
    before inline parsing) keep their brackets as written. The link reads the
    label, or else the target, as Markdown link text does: emphasis in it is
    rendered, ``<`` and ``>`` are shown as written, and backslash escapes hold.
    The slug is made from the target as the author wrote it.
    """

    def __init__(self, md: _md.Markdown, project_key: str, found: list[tuple[str, str | None]] | None = None) -> None:
        super().__init__(_WIKI_LINK_RE.pattern, md)
        self.project_key = project_key
        self.found = found

    def _written(self, text: str) -> str:
        """Return *text* with its escapes and code spans read back as the author typed them."""
        return _ESCAPED_CHAR_RE.sub(lambda e: "\\" + chr(int(e.group(1))), self.unescape(text))

    def handleMatch(  # noqa: N802 - Python-Markdown API
        self, m: re.Match[str], data: str
    ) -> tuple[Element | None, int | None, int | None]:
        target = m.group(1).strip()
        label = (m.group(2) or "").strip() or target
        written = self._written(target)
        if self.found is not None:
            self.found.append((written, self._written(m.group(2)).strip() if m.group(2) else None))
        element = Element("a", {"href": f"/projects/{self.project_key}/wiki/{_wiki_slugify(written)}/"})
        # As entities, so the entity pattern shows them as written rather than
        # the inline HTML pattern reading a tag.
        element.text = label.replace("<", "&lt;").replace(">", "&gt;")
        return element, m.start(0), m.end(0)


class _WikiLinkExtension(Extension):
    def __init__(self, project_key: str, found: list[tuple[str, str | None]] | None = None) -> None:
        super().__init__()
        self.project_key = project_key
        self.found = found

    def extendMarkdown(self, md: _md.Markdown) -> None:  # noqa: N802 - Python-Markdown API
        # Below backticks (190) and escapes (180), so code spans and escaped
        # brackets are settled first; above references (170) and links (160),
        # which would otherwise read ``[[Page]]`` as bracketed text.
        md.inlinePatterns.register(_WikiLinkInlineProcessor(md, self.project_key, self.found), "wiki_links", 175)


def find_wiki_links(text: str) -> list[tuple[str, str | None]]:
    """Return the ``(target, label or None)`` of each ``[[Page]]`` link the renderer makes of *text*, in order.

    Found by the parser, as rendering finds them, so a link written in code or
    in a raw HTML block is not one. Text the renderer would show as plain text
    (over the render budget, see :func:`render_hazard`), or whose parse runs
    out of time, is scanned as written instead, code and all.
    """
    if not text or "[[" not in text:
        return []
    if render_hazard(text, count_issue_ref_candidates(text)) is None:
        found: list[tuple[str, str | None]] = []
        started = markdown_budget._cpu_clock()
        try:
            _md.markdown(
                text,
                extensions=["fenced_code", "tables", _WikiLinkExtension("", found), RenderDeadline(render_allowance())],
            )
        except (RenderDeadlineError, RecursionError):
            pass
        else:
            return found
        finally:
            charge_render(markdown_budget._cpu_clock() - started)
    return [(m.group(1).strip(), m.group(2).strip() if m.group(2) else None) for m in _WIKI_LINK_RE.finditer(text)]


def render_wiki_markdown(
    text: str,
    project_key: str = "",
    attachment_map: dict | None = None,
    known_issue_refs: set[str] | None = None,
) -> markupsafe.Markup:
    """Render markdown with wiki ``[[Page Name]]`` links and ``KEY-123`` autolinks.

    This is the same pipeline used by the Jinja ``wiki_markdown`` filter
    (issue descriptions, comments, wiki bodies, description diffs).

    ``project_key``: used to build the wiki link URL. If empty, ``[[Page]]``
    links resolve to ``/projects//wiki/page/`` — fine for the preview
    endpoint where no project context is supplied; the user sees the link
    target as-it-would-render and any project mismatch surfaces immediately.

    ``attachment_map``: optional dict mapping filenames to attachment download
    URLs. When provided, bare-filename image and link targets like
    ``![alt](chart.png)`` or ``[the spec](spec.pdf)`` are rewritten to the
    attachment URL. Callers build it from the attachments the reader may
    open, so a hidden file never resolves.

    ``known_issue_refs``: optional set of ``KEY-123`` references that actually
    exist (or previously existed, via a move alias). When provided, ONLY those
    references are auto-linked and other ``KEY-123``-looking tokens are left as
    plain text. When ``None`` (the default), every matching token is linked —
    the original behaviour, kept so callers that cannot resolve refs (e.g. the
    editor preview without context) are unaffected.
    """
    if not text:
        return markupsafe.Markup("")

    # Wiki links and issue refs are linked, and bare-filename image and link
    # targets resolved to attachment URLs, on the parsed text, not by
    # rewriting the source.
    extensions: list = [*_MD_EXTENSIONS, _WikiLinkExtension(project_key), _IssueRefExtension(known_issue_refs)]
    if attachment_map:
        extensions.append(_AttachmentRefExtension(attachment_map))
    return _render_markdown(text, extensions, count_issue_ref_candidates(text))


def render_plain_markdown(text: str) -> markupsafe.Markup:
    """Render markdown without wiki link / autolink processing.

    Backs the Jinja ``markdown`` filter (used by the version detail page).
    Same extensions and sanitization as ``render_wiki_markdown``; only the
    pre-processing differs.
    """
    return _render_markdown(text or "", list(_MD_EXTENSIONS))
