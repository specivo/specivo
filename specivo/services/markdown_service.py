"""Server-side markdown renderer — single source of truth for HTML output.

Both the Jinja ``markdown`` / ``wiki_markdown`` filters (used during template
rendering) and the ``POST /api/v1/markdown/preview`` endpoint (used by the
editor live preview) call into this module. Keeping a single implementation
prevents drift between saved content and editor preview — the entire point
of doing preview server-side rather than shipping a second renderer to the
browser.

Pipeline:

    raw markdown
      -> wiki link substitution    [[Page]]    -> [Page](/projects/X/wiki/page/)
      -> issue ref auto-link       PROJ-123    -> [PROJ-123](/issue/PROJ-123/)
      -> render_hazard(...)        over the size, nesting or cost budget:
                                   the raw text, escaped, with a notice instead
      -> markdown.markdown(...)    with fenced_code, tables, toc, codehilite
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
from markdown.treeprocessors import Treeprocessor  # type: ignore[import-untyped]

from specivo.core.i18n import gettext as _
from specivo.services.markdown_budget import RenderDeadline, RenderDeadlineError, render_hazard
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
# Issue reference: PROJ-123, but not inside markdown links or wiki links
_ISSUE_REF_RE = re.compile(r"(?<!\[)(?<!\()(?<!/)\b([A-Z][A-Z0-9]+-\d+)\b")


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


def _render_markdown(source: str, original: str, extensions: list) -> markupsafe.Markup:
    """Render *source* to sanitized HTML, or *original* as plain text if that is unsafe.

    *source* is the text after wiki-link and issue-ref substitution, which is
    what Python-Markdown parses; *original* is what the author wrote, shown
    as-is in the fallback.
    """
    hazard = render_hazard(source, len(original))
    if hazard is None:
        try:
            html = _md.markdown(
                source,
                extensions=[*extensions, RenderDeadline()],
                extension_configs=_MD_EXTENSION_CONFIGS,
            )
        except RenderDeadlineError:
            hazard = "deadline"
        except RecursionError:
            hazard = "recursion"
        else:
            return markupsafe.Markup(_sanitize_html(html))
    logger.warning("Markdown shown as plain text (%s, %d characters)", hazard, len(original))
    return _plain_text_fallback(original)


def _plain_text_fallback(text: str) -> markupsafe.Markup:
    """Show *text* escaped and preformatted, below a notice saying why."""
    notice = _("This text is too complex to format, so it is shown as plain text.")
    return markupsafe.Markup(
        '<div class="sp-markdown-fallback"><p class="sp-markdown-fallback-notice">{}</p><pre>{}</pre></div>'
    ).format(notice, text)


def _replace_wiki_link(project_key: str):
    def _inner(m: re.Match[str]) -> str:
        target = m.group(1).strip()
        display = m.group(2)
        if display:
            display = display.strip()
            display = display.replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;").replace(">", "&gt;")
        else:
            display = target
            display = display.replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;").replace(">", "&gt;")
        slug = _wiki_slugify(target)
        url = f"/projects/{project_key}/wiki/{slug}/"
        return f"[{display}]({url})"

    return _inner


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


def substitute_links(text: str, project_key: str = "", known_issue_refs: set[str] | None = None) -> str:
    """Rewrite ``[[Page]]`` wiki links and ``KEY-123`` refs in *text* as Markdown links.

    The first step of :func:`render_wiki_markdown`, and the text the render
    budget is checked against: what Python-Markdown will actually parse.
    """
    processed = _WIKI_LINK_RE.sub(_replace_wiki_link(project_key), text)

    # Auto-link issue references: PROJ-123 -> [PROJ-123](/issue/PROJ-123/).
    # When known_issue_refs is provided, only link references that resolve to a
    # real (or previously-existing) issue; leave the rest as plain text.
    def _link_issue_ref(m: re.Match[str]) -> str:
        ref = m.group(1)
        if known_issue_refs is None or ref in known_issue_refs:
            return f"[{ref}](/issue/{ref}/)"
        return ref

    processed = _ISSUE_REF_RE.sub(_link_issue_ref, processed)
    return processed


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

    processed = substitute_links(text, project_key, known_issue_refs)

    # Bare-filename image and link targets are resolved to attachment URLs on
    # the parsed tree, not by rewriting the source.
    extensions: list = list(_MD_EXTENSIONS)
    if attachment_map:
        extensions.append(_AttachmentRefExtension(attachment_map))
    return _render_markdown(processed, text, extensions)


def render_plain_markdown(text: str) -> markupsafe.Markup:
    """Render markdown without wiki link / autolink processing.

    Backs the Jinja ``markdown`` filter (used by the version detail page).
    Same extensions and sanitization as ``render_wiki_markdown``; only the
    pre-processing differs.
    """
    return _render_markdown(text or "", text or "", list(_MD_EXTENSIONS))
