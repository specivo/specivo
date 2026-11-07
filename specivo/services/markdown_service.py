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
           + mermaid fences: ```mermaid -> <pre class="sp-mermaid">source</pre>
             (the browser draws the diagram, see frontend/js/modules/mermaid.js)
           + wiki link inline pattern: [[Page]] -> <a href="/projects/X/wiki/page/">
           + issue ref inline pattern: PROJ-123 -> <a href="/issue/PROJ-123/">
           + bare URL inline pattern: https://x.example/a -> <a href="https://x.example/a">
             (all three on parsed text only, so code and raw HTML blocks are
             never touched; issue refs and URLs stay out of link text too)
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
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit
from xml.etree.ElementTree import Element

import markdown as _md
import markupsafe
import nh3
from markdown.extensions import Extension  # type: ignore[import-untyped]
from markdown.extensions.attr_list import get_attrs_and_remainder  # type: ignore[import-untyped]
from markdown.extensions.fenced_code import FencedBlockPreprocessor  # type: ignore[import-untyped]
from markdown.extensions.tables import TableExtension, TableProcessor  # type: ignore[import-untyped]
from markdown.inlinepatterns import InlineProcessor  # type: ignore[import-untyped]
from markdown.preprocessors import Preprocessor  # type: ignore[import-untyped]
from markdown.treeprocessors import Treeprocessor  # type: ignore[import-untyped]
from markdown.util import HTML_PLACEHOLDER_RE, AtomicString  # type: ignore[import-untyped]

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


class _MermaidPreprocessor(Preprocessor):
    """Stash each ```` ```mermaid ```` fence as ``<pre class="sp-mermaid">`` with its source escaped.

    Runs just before ``fenced_code`` and walks the fences as it does, so a
    mermaid fence inside a longer fence stays code. The source is kept as
    text: search, exports and MCP reads see it, and the browser module that
    draws the diagram reads it back with ``textContent``. Other fences are
    left for ``fenced_code``.
    """

    def run(self, lines: list[str]) -> list[str]:
        text = "\n".join(lines)
        index = 0
        while m := FencedBlockPreprocessor.FENCED_BLOCK_RE.search(text, index):
            lang = m.group("lang")
            if m.group("attrs"):
                attrs, remainder = get_attrs_and_remainder(m.group("attrs"))
                if remainder:
                    # fenced_code skips a malformed attribute list the same way.
                    index = m.end("attrs")
                    continue
                classes = [value for key, value in attrs if key == "."]
                lang = classes[0] if classes else None
            if lang != "mermaid":
                index = m.end()
                continue
            html = markupsafe.Markup('<pre class="sp-mermaid">{}</pre>').format(m.group("code"))
            placeholder = self.md.htmlStash.store(str(html))
            text = f"{text[: m.start()]}\n{placeholder}\n{text[m.end() :]}"
            index = m.start() + 1 + len(placeholder)
        return text.split("\n")


class _MermaidExtension(Extension):
    def extendMarkdown(self, md: _md.Markdown) -> None:  # noqa: N802 - Python-Markdown API
        # Just above fenced_code (25), so mermaid fences are taken first.
        md.preprocessors.register(_MermaidPreprocessor(md), "mermaid_fences", 26)


class _WikiTableProcessor(TableProcessor):
    """The ``tables`` processor, reading ``[[Page|label]]`` as one cell.

    Wiki links are made inline, after the table has split its rows, so the
    ``|`` between a link's target and label would otherwise end the cell. It
    is passed over as a ``|`` in a code span is.
    """

    def _split(self, row: str) -> list[str]:
        # Same length, so each cell maps back to its span of the row as written.
        masked = _WIKI_LINK_RE.sub(lambda m: m.group(0).replace("|", "\x00"), row)
        cells = []
        pos = 0
        for cell in super()._split(masked):
            cells.append(row[pos : pos + len(cell)])
            pos += len(cell) + 1
        return cells


class _WikiTableExtension(TableExtension):
    def extendMarkdown(self, md: _md.Markdown) -> None:  # noqa: N802 - Python-Markdown API
        super().extendMarkdown(md)
        # Replaces the stock processor under its own name and priority.
        md.parser.blockprocessors.register(_WikiTableProcessor(md.parser, self.getConfigs()), "table", 75)


# A bare URL: ``http://`` or ``https://``, ``www.`` or ``mailto:``, at the start
# of the text or after a blank, an opening bracket or quote, an emphasis marker
# or an inline element (whose placeholder ends in ``\x03``), and running to the
# next blank, ``<``, ``>`` or placeholder. One scan to the end of the run per
# match, so linear; :func:`_trim_bare_url` then cuts it to what is linked.
BARE_URL_PATTERN = r"(?<![^\s(\[{*_~\"'\x03])((?:https?://|www\.|mailto:)[^\s<>\x02\x03]+)"
# Host of an http(s) or www URL: dot-separated labels and an optional port.
# No userinfo (``user:pass@``), so a link never hides a different host.
_URL_HOST_RE = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.?(?::\d{1,5})?")
_MAILTO_RE = re.compile(r"mailto:[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# Characters a URL may not end in: they read as the sentence's punctuation.
_URL_TRAILING_PUNCTUATION = "?!.,:;*_~'\""
# An entity reference (``&amp;``) at the end of a URL is text after it.
_URL_TRAILING_ENTITY_RE = re.compile(r"&[A-Za-z0-9]+;$")
# A raw inline ``<a ...>`` or ``</a>`` tag, as Python-Markdown stashes it.
_RAW_ANCHOR_RE = re.compile(r"<(/?)a(?:\s[^>]*)?>", re.IGNORECASE)
_INLINE_PLACEHOLDER_PREFIX = "\x02klzzwxh:"


def _trim_bare_url(url: str) -> str:
    """Cut what GFM leaves out of an autolink off the end of *url*.

    Trailing punctuation (``.``, ``,``, ``?``, quotes, ...) and an entity
    reference are dropped, and so is a closing ``)`` without a matching ``(``
    in the URL. Each step removes at least one character, so this is linear.
    """
    opened = url.count("(")
    closed = url.count(")")
    while url:
        last = url[-1]
        if last == ";" and (entity := _URL_TRAILING_ENTITY_RE.search(url[-33:])):
            url = url[: len(url) - len(entity.group())]
        elif last in _URL_TRAILING_PUNCTUATION:
            url = url[:-1]
        elif last == ")" and closed > opened:
            url = url[:-1]
            closed -= 1
        else:
            break
    return url


def bare_url_href(url: str) -> str | None:
    """Return the ``href`` for bare *url* (already trimmed), or None if it is not linked.

    ``www.`` gets an ``http://`` scheme. An http(s) or www URL needs a host
    (labels and an optional port, no user info); a ``mailto:`` URL needs an
    address with a domain.
    """
    if url.startswith("mailto:"):
        return url if _MAILTO_RE.fullmatch(url) else None
    if url.startswith("www."):
        href = "http://" + url
        rest = url
    else:
        href = url
        rest = url.split("://", 1)[1]
    host = re.split(r"[/?#]", rest, maxsplit=1)[0]
    if not host or not _URL_HOST_RE.fullmatch(host):
        return None
    if url.startswith("www.") and "." not in host[len("www.") :].strip("."):
        return None
    return href


class _BareUrlInlineProcessor(InlineProcessor):
    """Link bare ``https://``, ``http://``, ``www.`` and ``mailto:`` URLs in parsed text.

    An inline pattern only sees text Python-Markdown parses as Markdown, so
    code spans, fenced and indented code blocks and raw HTML blocks keep their
    URLs as written. ``ANCESTOR_EXCLUDES`` keeps it out of the text of a
    Markdown link or ``<url>`` autolink, and a URL between a raw inline
    ``<a ...>`` and its ``</a>`` is left alone too. Only http, https and
    mailto targets are made; the sanitizer enforces the same list.
    """

    ANCESTOR_EXCLUDES = ("a",)

    def __init__(self, md: _md.Markdown) -> None:
        super().__init__(BARE_URL_PATTERN, md)
        # Whether the text right after an inline placeholder is inside a raw
        # ``<a>``, by placeholder id. Each placeholder is looked at once, so
        # the checks of all the URLs in a text add up to one pass over it.
        self._inside_anchor_after: dict[str, bool] = {}
        self._stash_seen: object = None

    def _raw_anchor_tag(self, node: object) -> bool | None:
        """True for a stashed raw ``<a ...>``, False for ``</a>``, None for anything else."""
        if not isinstance(node, str):
            return None
        m = HTML_PLACEHOLDER_RE.fullmatch(node)
        if m is None:
            return None
        index = int(m.group(1))
        blocks = self.md.htmlStash.rawHtmlBlocks
        if index >= len(blocks):
            return None
        tag = _RAW_ANCHOR_RE.fullmatch(str(blocks[index]).strip())
        return None if tag is None else not tag.group(1)

    def _inside_raw_anchor(self, data: str, pos: int) -> bool:
        """Whether *pos* in *data* follows a raw inline ``<a ...>`` not closed before it."""
        stash = self.md.treeprocessors["inline"].stashed_nodes
        if stash is not self._stash_seen:
            self._stash_seen = stash
            self._inside_anchor_after.clear()
        known = self._inside_anchor_after
        unseen: list[str] = []
        inside = False
        end = pos
        while (start := data.rfind(_INLINE_PLACEHOLDER_PREFIX, 0, end)) >= 0:
            close = data.find("\x03", start, end)
            key = data[start + len(_INLINE_PLACEHOLDER_PREFIX) : close] if close >= 0 else ""
            if key in known:
                inside = known[key]
                break
            unseen.append(key)
            end = start
        for key in reversed(unseen):
            tag = self._raw_anchor_tag(stash.get(key))
            if tag is not None:
                inside = tag
            known[key] = inside
        return inside

    def handleMatch(  # noqa: N802 - Python-Markdown API
        self, m: re.Match[str], data: str
    ) -> tuple[Element | None, int | None, int | None]:
        url = _trim_bare_url(m.group(1))
        href = bare_url_href(url) if url else None
        if href is None or self._inside_raw_anchor(data, m.start(1)):
            return None, None, None
        element = Element("a", {"href": href})
        # Atomic, so issue refs, wiki links and emphasis are not looked for in it.
        element.text = AtomicString(url)
        return element, m.start(1), m.start(1) + len(url)


class _BareUrlExtension(Extension):
    def extendMarkdown(self, md: _md.Markdown) -> None:  # noqa: N802 - Python-Markdown API
        # Below links (160), ``<url>`` autolinks (120), inline HTML (90) and
        # entities (80), so those are settled first; above emphasis (60), so
        # ``*`` and ``_`` in a URL stay part of it.
        md.inlinePatterns.register(_BareUrlInlineProcessor(md), "bare_urls", 75)


_MD_EXTENSIONS: list = [
    "fenced_code",
    _WikiTableExtension(),
    "toc",
    "codehilite",
    _MermaidExtension(),
    _BareUrlExtension(),
]
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

    def __init__(
        self,
        md: _md.Markdown,
        project_key: str,
        found: list[tuple[str, str | None]] | None = None,
        made: set[int] | None = None,
    ) -> None:
        super().__init__(_WIKI_LINK_RE.pattern, md)
        self.project_key = project_key
        self.found = found
        self.made = made

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
        if self.made is not None:
            self.made.add(id(element))
        return element, m.start(0), m.end(0)


class _WikiLinkExtension(Extension):
    def __init__(
        self,
        project_key: str,
        found: list[tuple[str, str | None]] | None = None,
        made: set[int] | None = None,
    ) -> None:
        super().__init__()
        self.project_key = project_key
        self.found = found
        self.made = made

    def extendMarkdown(self, md: _md.Markdown) -> None:  # noqa: N802 - Python-Markdown API
        # Below backticks (190) and escapes (180), so code spans and escaped
        # brackets are settled first; above references (170) and links (160),
        # which would otherwise read ``[[Page]]`` as bracketed text.
        md.inlinePatterns.register(
            _WikiLinkInlineProcessor(md, self.project_key, self.found, self.made), "wiki_links", 175
        )


@dataclass(frozen=True, slots=True)
class FoundLink:
    """A link :func:`extract_links` found in a text.

    ``kind`` is ``"wiki"`` (``target`` is a page slug, in ``project_key``'s
    wiki, or the text's own wiki when ``project_key`` is None), ``"issue"``
    (``target`` is ``KEY-123``), ``"file"`` (``target`` is a bare attachment
    filename) or ``"attachment"`` (``target`` is an attachment id).
    """

    kind: str
    target: str
    label: str | None = None
    project_key: str | None = None


# Paths of this instance a link may point at. The wiki route's literal
# segments (``new``, ``pages``, ``trash``) are pages of the wiki, not slugs.
_ISSUE_PATH_RE = re.compile(r"^/(?:issue|projects/[A-Za-z0-9_-]+/issues)/([A-Z][A-Z0-9]+-\d+)(?:/|$)")
_WIKI_PATH_RE = re.compile(r"^/projects/([A-Za-z0-9_-]+)/wiki/([A-Za-z0-9-]+)(?:/|$)")
_ATTACHMENT_PATH_RE = re.compile(r"^/attachments/(\d+)(?:/|$)")
_WIKI_RESERVED_SEGMENTS = frozenset({"new", "pages", "trash", "graph"})


def _origin(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def classify_link_target(target: str, instance_origin: str | None = None) -> FoundLink | None:
    """Return what an ``href``/``src`` *target* points at in this instance, or None.

    A bare filename names an attachment. Otherwise the target must be a path
    on this instance: relative (``/issue/ACME-1/``) or absolute on
    *instance_origin* (``https://tracker.example.com/issue/ACME-1/``), and
    point at an issue, a wiki page or an attachment.
    """
    filename = _attachment_filename(target)
    if filename is not None:
        return FoundLink("file", filename)
    parts = urlsplit(target.strip())
    if parts.scheme or parts.netloc:
        origin = _origin(target.strip())
        if origin is None or instance_origin is None or origin != _origin(instance_origin):
            return None
    path = unquote(parts.path)
    if m := _ISSUE_PATH_RE.match(path):
        return FoundLink("issue", m.group(1))
    if m := _WIKI_PATH_RE.match(path):
        if m.group(2).lower() in _WIKI_RESERVED_SEGMENTS:
            return None
        return FoundLink("wiki", m.group(2).lower(), project_key=m.group(1).upper())
    if m := _ATTACHMENT_PATH_RE.match(path):
        return FoundLink("attachment", m.group(1))
    return None


class _LinkCollector(Treeprocessor):
    """Record every ``<a href>`` and ``<img src>`` the parse made, except ``[[...]]`` links (found apart)."""

    def __init__(self, md: _md.Markdown, found: list[FoundLink], skip: set[int], instance_origin: str | None) -> None:
        super().__init__(md)
        self.found = found
        self.skip = skip
        self.instance_origin = instance_origin

    def run(self, root: Element) -> None:
        for element in root.iter():
            attribute = {"img": "src", "a": "href"}.get(element.tag)
            if attribute is None or id(element) in self.skip:
                continue
            target = element.get(attribute)
            if not target:
                continue
            link = classify_link_target(target, self.instance_origin)
            if link is None:
                continue
            label = element.get("alt") if element.tag == "img" else "".join(element.itertext())
            label = (label or "").strip() or None
            if label in (link.target, target):
                label = None
            self.found.append(FoundLink(link.kind, link.target, label=label, project_key=link.project_key))


class _LinkCollectorExtension(Extension):
    def __init__(self, found: list[FoundLink], skip: set[int], instance_origin: str | None) -> None:
        super().__init__()
        self.found = found
        self.skip = skip
        self.instance_origin = instance_origin

    def extendMarkdown(self, md: _md.Markdown) -> None:  # noqa: N802 - Python-Markdown API
        # Below "inline" (20), so wiki links and issue refs are elements by now.
        md.treeprocessors.register(_LinkCollector(md, self.found, self.skip, self.instance_origin), "link_collector", 4)


def extract_links(text: str | None, instance_origin: str | None = None) -> list[FoundLink]:
    """Return the links the renderer makes of *text*, in order, duplicates included.

    Parsed with the renderer's own wiki-link and issue-ref patterns, so a link
    in a code span or block, in a raw HTML block or (for issue refs) in link
    text is not one, exactly as on the page. ``[[Page]]`` targets come back as
    slugs, ``KEY-123`` tokens and links to ``/issue/KEY-123/`` as issue refs,
    links to a wiki page or an attachment of this instance (relative, or
    absolute on *instance_origin*) as such, and bare-filename link and image
    targets as attachment filenames. A bare URL counts as the link the
    renderer makes of it, so ``https://<instance>/issue/ACME-1/`` written as
    plain text is a link to ACME-1.

    Text the renderer would show as plain text (over the render budget, see
    :func:`render_hazard`), or whose parse runs out of time, is scanned as
    written instead for ``[[...]]`` links and ``KEY-123`` tokens, code and all.
    """
    if not text:
        return []
    if render_hazard(text, count_issue_ref_candidates(text)) is None:
        wiki_found: list[tuple[str, str | None]] = []
        found: list[FoundLink] = []
        made: set[int] = set()
        started = markdown_budget._cpu_clock()
        try:
            _md.markdown(
                text,
                extensions=[
                    "fenced_code",
                    _WikiTableExtension(),
                    _WikiLinkExtension("", wiki_found, made),
                    _IssueRefExtension(None),
                    _BareUrlExtension(),
                    _LinkCollectorExtension(found, made, instance_origin),
                    RenderDeadline(render_allowance()),
                ],
            )
        except (RenderDeadlineError, RecursionError):
            pass
        else:
            wiki = [FoundLink("wiki", _wiki_slugify(target), label=label) for target, label in wiki_found]
            return wiki + found
        finally:
            charge_render(markdown_budget._cpu_clock() - started)
    links = [
        FoundLink(
            "wiki",
            _wiki_slugify(m.group(1).strip()),
            label=m.group(2).strip() if m.group(2) else None,
        )
        for m in _WIKI_LINK_RE.finditer(text)
    ]
    links.extend(FoundLink("issue", m.group(1)) for m in _ISSUE_REF_RE.finditer(text))
    return links


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
