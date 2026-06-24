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
      -> attachment image rewrite  ![a](b.png) -> ![a](/path/to/b.png)
      -> issue ref auto-link       PROJ-123    -> [PROJ-123](/issue/PROJ-123/)
      -> markdown.markdown(...)    with fenced_code, tables, toc, codehilite
      -> nh3.clean(...)            HTML sanitizer with our allowlist
      -> markupsafe.Markup
"""

from __future__ import annotations

import re

import markdown as _md
import markupsafe
import nh3

from specivo.services.wiki_utils import slugify as _wiki_slugify

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
_WIKI_LINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")
_IMG_SRC_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
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


def _replace_wiki_link(project_key: str):
    def _inner(m: re.Match[str]) -> str:
        target = m.group(1).strip()
        display = m.group(2)
        if display:
            display = display.strip()
            display = (
                display.replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;").replace(">", "&gt;")
            )
        else:
            display = target
            display = (
                display.replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;").replace(">", "&gt;")
            )
        slug = _wiki_slugify(target)
        url = f"/projects/{project_key}/wiki/{slug}/"
        return f"[{display}]({url})"

    return _inner


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
    URLs. When provided, bare-filename image references like
    ``![alt](chart.png)`` are rewritten to the attachment URL.

    ``known_issue_refs``: optional set of ``KEY-123`` references that actually
    exist (or previously existed, via a move alias). When provided, ONLY those
    references are auto-linked and other ``KEY-123``-looking tokens are left as
    plain text. When ``None`` (the default), every matching token is linked —
    the original behaviour, kept so callers that cannot resolve refs (e.g. the
    editor preview without context) are unaffected.
    """
    if not text:
        return markupsafe.Markup("")

    processed = _WIKI_LINK_RE.sub(_replace_wiki_link(project_key), text)

    # Rewrite bare-filename image references to attachment URLs
    if attachment_map:

        def _resolve_image(m: re.Match[str]) -> str:
            alt, src = m.group(1), m.group(2)
            if src in attachment_map:
                return f"![{alt}]({attachment_map[src]})"
            return m.group(0)

        processed = _IMG_SRC_RE.sub(_resolve_image, processed)

    # Auto-link issue references: PROJ-123 -> [PROJ-123](/issue/PROJ-123/).
    # When known_issue_refs is provided, only link references that resolve to a
    # real (or previously-existing) issue; leave the rest as plain text.
    def _link_issue_ref(m: re.Match[str]) -> str:
        ref = m.group(1)
        if known_issue_refs is None or ref in known_issue_refs:
            return f"[{ref}](/issue/{ref}/)"
        return ref

    processed = _ISSUE_REF_RE.sub(_link_issue_ref, processed)

    rendered = _md.markdown(
        processed,
        extensions=_MD_EXTENSIONS,
        extension_configs=_MD_EXTENSION_CONFIGS,
    )
    return markupsafe.Markup(_sanitize_html(rendered))


def render_plain_markdown(text: str) -> markupsafe.Markup:
    """Render markdown without wiki link / autolink processing.

    Backs the Jinja ``markdown`` filter (used by the version detail page).
    Same extensions and sanitization as ``render_wiki_markdown``; only the
    pre-processing differs.
    """
    html = _md.markdown(
        text or "",
        extensions=_MD_EXTENSIONS,
        extension_configs=_MD_EXTENSION_CONFIGS,
    )
    return markupsafe.Markup(_sanitize_html(html))
