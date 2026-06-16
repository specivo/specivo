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

    # Auto-link issue references: PROJ-123 -> [PROJ-123](/issue/PROJ-123/)
    processed = _ISSUE_REF_RE.sub(r"[\1](/issue/\1/)", processed)

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
