"""Markdown inputs for the render-budget tests.

``PATHOLOGICAL`` holds inputs that Python-Markdown renders in super-linear time
(or not at all, by exhausting the recursion limit). Each comment gives the
render time before the guard existed. Every input fits the stored-text limit,
so any author could save it as a comment, description or wiki page.

The realistic builders produce large documents that must keep rendering
normally.
"""

from __future__ import annotations

from specivo.services.markdown_budget import MAX_MARKDOWN_CHARS


def fill(unit: str, prefix: str = "", suffix: str = "", size: int = MAX_MARKDOWN_CHARS) -> str:
    """Repeat *unit* between *prefix* and *suffix* up to *size* characters."""
    return prefix + unit * ((size - len(prefix) - len(suffix)) // len(unit)) + suffix


PATHOLOGICAL: dict[str, str] = {
    # Unclosed link and image openers: 60 KB took 23 s, the full limit minutes.
    "unclosed-link-target": fill("[a](x "),
    "unclosed-image-target": fill("![a](x "),
    "unclosed-link-text": fill("[a "),
    "bare-brackets": fill("["),
    "bare-image-openers": fill("!["),
    "unclosed-reference": fill("[a]["),
    "empty-link-target": fill("[a]("),
    # Our own ``[[wiki link]]`` regex: 16 KB took 2.5 s before the regex itself was fixed.
    "wiki-link-openers": fill("[["),
    "wiki-link-pipes": fill("[[a|"),
    # Raw HTML openers without ``>``, scanned across the whole document.
    "unclosed-tags": fill("<a "),
    "unclosed-autolinks": fill("<http://x "),
    "unclosed-comments": fill("<!-- a\n"),
    # Backtick runs without a closing run of the same length.
    "backtick-run": fill("`"),
    # ``toc`` gives duplicate headings ids one probe at a time: 62 KB took 38 s.
    "duplicate-atx-headings": fill("# a\n"),
    "duplicate-setext-headings": fill("a\n-\n"),
    # Consecutive heading lines are split off one at a time: 16,000 took 4 s.
    "consecutive-headings": "".join(f"## Heading {i}\n" for i in range(MAX_MARKDOWN_CHARS // 18)),
    # A long run of ``#`` backtracks in the heading regex: 40 KB took 2.8 s.
    "hash-run": "#" * (MAX_MARKDOWN_CHARS // 2 - 10) + " a #" + "b" * (MAX_MARKDOWN_CHARS // 2 - 10),
    # Every ``[TOC]`` copies the whole table of contents: 68 KB took 9.6 s and made 163 MB of HTML.
    "toc-markers": "".join(f"## H{i}\n\n" for i in range(300)) + fill("[TOC]\n\n", size=MAX_MARKDOWN_CHARS // 2),
    # Deep nesting: a 2 KB list raised RecursionError; deep quotes took seconds.
    "nested-list-markers": fill("- ", suffix="a"),
    "nested-ordered-markers": fill("1. ", suffix="a"),
    "nested-quote-and-list": fill("> - ", suffix="a"),
    "nested-quote-markers": fill(">", suffix=" a"),
    "indented-lists": "".join("    " * i + "- a\n" for i in range(300)),
    # Linear, but many elements at the size limit took 1.5 to 4 s.
    "issue-refs": fill("ACME-1 "),
    "closed-wiki-links": fill("[[A]] "),
    "closed-inline-html": fill("<b>a</b> "),
    "empty-fenced-blocks": fill("~~~\n"),
    "escapes": fill("\\* "),
    "paragraphs": fill("a\n\n"),
}

# Nested emphasis exhausts the recursion limit inside the inline parser; the
# pre-scan does not model it, so the render itself falls back.
RECURSION = "*" * 5000 + "a" + "*" * 5000


def links_list(count: int = 2000) -> str:
    """A list of *count* links, each with an image."""
    return "\n".join(
        f"- see [doc {i}](https://example.com/{i}) and ![img {i}](https://example.com/{i}.png)" for i in range(count)
    )


def links_paragraph(count: int = 2000) -> str:
    """One paragraph holding *count* links."""
    return " ".join(f"see [doc {i}](https://example.com/{i})" for i in range(count))


def large_page(size: int = 200 * 1024) -> str:
    """A wiki page of about *size* characters using every common construct."""
    parts: list[str] = []
    total = 0
    i = 0
    while total < size:
        part = (
            f"## Section {i}\n\n"
            f"See [the docs](https://example.com/p/{i}) and ![chart](https://example.com/c{i}.png), "
            f"**bold** and *emphasis* with `code {i}`, ACME-{i} and [[Page {i}]].\n\n"
            f"- item [link](https://example.com/{i})\n- item two\n  - nested item\n\n"
            f"| key | value |\n|---|---|\n| {i} | [file](https://example.com/f{i}.pdf) |\n\n"
            f"```python\nprint({i})\n```\n\n> quote {i}\n\n"
        )
        parts.append(part)
        total += len(part)
        i += 1
    return "".join(parts)
