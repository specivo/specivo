"""Convert Redmine markup to the Markdown Specivo stores.

Redmine's historical markup is Textile, and modern instances store CommonMark
instead — Redmine 7 defaults to it. Both need the same link rewriting; only
Textile needs converting.

Conversion goes Textile to HTML to Markdown, using two mature libraries rather
than a hand-written Textile parser or an external binary. Redmine's dialect is
RedCloth-derived and full of corners, and the one thing a migration cannot do is
mangle a decade of issue descriptions.

Around that sit three passes that the libraries know nothing about:

**Protect.** Code blocks are lifted out before anything touches them, so
indentation and contents survive verbatim. Wiki links and issue references are
lifted out too: ``#123`` at the start of a line is an ordered list to Textile,
which would silently turn it into "123.".

**Rewrite.** Redmine's own macros — ``attachment:``, ``version:``, ``user:`` —
are translated while the text is still Textile, because the converter would
otherwise curl their quotes and make them unparseable.

**Restore.** The protected fragments come back as Markdown: code blocks as
fenced blocks, keeping the language when Redmine recorded one.

Nothing here raises. Unconvertible input comes back as a fenced block of the
original text, because losing a description is worse than an ugly one, and the
failure is counted for the import report.
"""

from __future__ import annotations

import logging
import re

from specivo.importers.core.converter import ConversionContext

logger = logging.getLogger(__name__)

# Formats that are already Markdown and only need link rewriting.
MARKDOWN_FORMATS = frozenset({"markdown", "common_mark", "commonmark"})

# Extensions Specivo's renderer can resolve as an inline image.
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp", ".ico", ".tif", ".tiff"})

# Placeholder tokens. Deliberately bare alphanumerics: anything with
# punctuation risks being escaped or reflowed by one of the two converters.
_TOKEN = "ZZSPVPROTECTED{index}ZZ"
_TOKEN_RE = re.compile(r"ZZSPVPROTECTED(\d+)ZZ")

# <pre><code class="ruby">...</code></pre> and the bare <pre> form. Redmine
# writes both, and Textile escapes the inner tag, which loses the language.
_PRE_CODE_RE = re.compile(
    r"<pre>\s*<code(?:\s+class=[\"']?([\w.+-]+)[\"']?)?\s*>(.*?)</code>\s*</pre>",
    re.DOTALL | re.IGNORECASE,
)
_PRE_RE = re.compile(r"<pre>(.*?)</pre>", re.DOTALL | re.IGNORECASE)
# Textile's own code block: "bc. " for one paragraph, "bc.. " until the next block.
_BC_RE = re.compile(r"^bc\.\.?\s?(.*?)(?=\n\n|\Z)", re.DOTALL | re.MULTILINE)

# [[Page]] and [[Page|Text]] — identical in both systems, so protected rather
# than rewritten.
_WIKI_LINK_RE = re.compile(r"\[\[[^\]\n]+\]\]")

# Redmine's issue reference. Protected because a line starting with # is an
# ordered list in Textile. Rewritten to a Specivo display key later in the
# import, once every issue exists.
_ISSUE_REF_RE = re.compile(r"(?<![\w&])#\d+\b")

# attachment:name.ext, attachment:"name with spaces.ext"
_ATTACHMENT_RE = re.compile(r'attachment:(?:"([^"\n]+)"|([^\s,;)\]]+))')
# version:"1.0" / version:1.0 — no Specivo equivalent, so reduced to its name.
_VERSION_RE = re.compile(r'version:(?:"([^"\n]+)"|([^\s,;)\]]+))')
# user:login becomes a Specivo mention, which is the same idea.
_USER_RE = re.compile(r"user:([A-Za-z0-9_.\-]+)")

# Three or more blank lines add nothing and make diffs noisy.
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")


class RedmineTextileConverter:
    """Converts one field of Redmine markup to Markdown.

    Instances accumulate a failure count, so a run can report how many fields
    fell back to raw text rather than silently shipping them.
    """

    def __init__(self) -> None:
        self.failure_count = 0
        self.failed_samples: list[str] = []

    def convert(self, raw: str, ctx: ConversionContext | None = None) -> str:
        """Return *raw* as Markdown. Never raises."""
        if not raw or not raw.strip():
            return ""

        ctx = ctx or ConversionContext()
        try:
            protected: dict[str, str] = {}
            text = self._protect_code(raw, protected)
            text = self._rewrite_macros(text, protected)
            text = self._protect_spans(text, protected)

            if ctx.source_format.lower() not in MARKDOWN_FORMATS:
                text = self._textile_to_markdown(text)

            text = self._restore(text, protected)
            return _EXCESS_BLANK_LINES_RE.sub("\n\n", text).strip()
        except Exception as exc:  # noqa: BLE001 - a bad field must not stop an import
            self.failure_count += 1
            if len(self.failed_samples) < 20:
                self.failed_samples.append(raw[:80])
            logger.warning("Markup conversion failed (%s); keeping the original text", exc)
            return self._fallback(raw)

    # ------------------------------------------------------------------
    # Protect
    # ------------------------------------------------------------------

    def _protect_code(self, text: str, protected: dict[str, str]) -> str:
        """Replace code blocks with tokens, remembering them as fenced Markdown."""

        def store(fence: str) -> str:
            token = _TOKEN.format(index=len(protected))
            protected[token] = fence
            return f"\n\n{token}\n\n"

        def on_pre_code(match: re.Match[str]) -> str:
            language = (match.group(1) or "").strip()
            body = _unescape(match.group(2)).strip("\n")
            return store(f"```{language}\n{body}\n```")

        def on_pre(match: re.Match[str]) -> str:
            body = _unescape(match.group(1)).strip("\n")
            return store(f"```\n{body}\n```")

        def on_bc(match: re.Match[str]) -> str:
            return store(f"```\n{match.group(1).strip()}\n```")

        text = _PRE_CODE_RE.sub(on_pre_code, text)
        text = _PRE_RE.sub(on_pre, text)
        return _BC_RE.sub(on_bc, text)

    def _protect_spans(self, text: str, protected: dict[str, str]) -> str:
        """Replace wiki links and issue references with tokens, verbatim."""

        def store(match: re.Match[str]) -> str:
            token = _TOKEN.format(index=len(protected))
            protected[token] = match.group(0)
            return token

        text = _WIKI_LINK_RE.sub(store, text)
        return _ISSUE_REF_RE.sub(store, text)

    def _restore(self, text: str, protected: dict[str, str]) -> str:
        """Put every protected fragment back."""

        def put_back(match: re.Match[str]) -> str:
            return protected.get(match.group(0), match.group(0))

        # Converters may escape or re-wrap around a token, so restore until
        # nothing changes rather than assuming one pass is enough.
        for _ in range(3):
            replaced = _TOKEN_RE.sub(put_back, text)
            if replaced == text:
                break
            text = replaced
        return text

    # ------------------------------------------------------------------
    # Rewrite
    # ------------------------------------------------------------------

    def _rewrite_macros(self, text: str, protected: dict[str, str]) -> str:
        """Translate Redmine's link macros while the text is still Textile.

        Attachments become their final Markdown form immediately and are
        protected, rather than being routed through Textile's image syntax:
        that syntax cannot carry a filename with a space in it, and a
        CommonMark source would keep the Textile markers verbatim.
        """

        def on_attachment(match: re.Match[str]) -> str:
            filename = match.group(1) or match.group(2)
            if not _is_image(filename):
                # Only images resolve by filename today, and a link that 404s
                # reads worse than a filename; the file is still attached.
                return filename
            token = _TOKEN.format(index=len(protected))
            # The bare-filename image form, which the renderer resolves against
            # the attachment map of the issue or page it is stored on.
            protected[token] = f"![{filename}]({filename})"
            return token

        def on_version(match: re.Match[str]) -> str:
            return match.group(1) or match.group(2)

        text = _ATTACHMENT_RE.sub(on_attachment, text)
        text = _VERSION_RE.sub(on_version, text)
        return _USER_RE.sub(lambda m: f"@{m.group(1)}", text)

    # ------------------------------------------------------------------
    # Convert
    # ------------------------------------------------------------------

    def _textile_to_markdown(self, text: str) -> str:
        """Run the two-hop conversion, sanitising the HTML in between."""
        import nh3
        import textile
        from markdownify import markdownify

        from specivo.services.markdown_service import _ALLOWED_ATTRIBUTES, _ALLOWED_TAGS

        html = textile.textile(text)
        # Sanitise here rather than trusting the render path to do it later: a
        # tag Specivo would drop should not reach the stored content, and this
        # way the same allowlist governs both.
        html = nh3.clean(html, tags=set(_ALLOWED_TAGS), attributes={k: set(v) for k, v in _ALLOWED_ATTRIBUTES.items()})
        return markdownify(html, heading_style="ATX", bullets="-")

    def _fallback(self, raw: str) -> str:
        """Return the original text in a fenced block, so nothing is lost."""
        fence = "````" if "```" in raw else "```"
        return f"{fence}\n{raw.strip()}\n{fence}"


def _is_image(filename: str) -> bool:
    """Whether *filename* is one Specivo's renderer can show inline."""
    lowered = filename.lower()
    return any(lowered.endswith(extension) for extension in _IMAGE_EXTENSIONS)


def _unescape(text: str) -> str:
    """Undo the entity escaping Redmine applies inside code blocks."""
    import html as _html

    return _html.unescape(text)


def make_converter(source_format: str) -> tuple[RedmineTextileConverter, ConversionContext]:
    """Return a converter and the context describing *source_format*."""
    return RedmineTextileConverter(), ConversionContext(source_format=source_format)
