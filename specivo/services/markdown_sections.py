"""Split Markdown into heading sections, for tools that read or replace one section of a page."""

from __future__ import annotations

import re

_HEADING_RE = re.compile(r"^(#{1,6}) (.+)$")
# An opening or closing code fence: up to three spaces, then ``` or ~~~ (three or more).
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def split_into_sections(text: str) -> list[dict]:
    """Split markdown into sections by headings.

    Returns list of {"heading": str|None, "level": int, "body": str, "start": int, "end": int}.
    First section may have heading=None for content before any heading.
    A ``#`` line inside a fenced code block (``` or ~~~) is code, not a heading.
    ``start`` and ``end`` are line indices (0-based) within the original text lines.
    """
    lines = text.split("\n")
    sections: list[dict] = []
    current_heading: str | None = None
    current_level: int = 0
    current_start: int = 0
    body_lines: list[str] = []
    fence: str | None = None  # the opening fence while inside a code block

    for idx, line in enumerate(lines):
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not line.strip()[len(marker) :].strip():
                fence = None
        m = None if fence is not None or fence_match else _HEADING_RE.match(line)
        if m:
            # Flush previous section
            sections.append(
                {
                    "heading": current_heading,
                    "level": current_level,
                    "body": "\n".join(body_lines),
                    "start": current_start,
                    "end": idx - 1 if idx > 0 else 0,
                }
            )
            current_heading = line
            current_level = len(m.group(1))
            current_start = idx
            body_lines = []
        else:
            body_lines.append(line)

    # Flush last section
    sections.append(
        {
            "heading": current_heading,
            "level": current_level,
            "body": "\n".join(body_lines),
            "start": current_start,
            "end": len(lines) - 1,
        }
    )

    return sections


def find_section(
    sections: list[dict],
    heading: str,
) -> tuple[int, dict] | None:
    """Find a section by heading text.

    Accepts both ``## Foo`` (exact heading line) and ``Foo`` (bare text, searches all levels).
    Returns (index, section_dict) or None.
    """
    # Exact heading line match first
    for idx, s in enumerate(sections):
        if s["heading"] == heading:
            return idx, s

    # Bare text: strip leading #s from heading and compare
    bare = heading.lstrip("#").strip()
    for idx, s in enumerate(sections):
        if s["heading"] is not None:
            section_bare = s["heading"].lstrip("#").strip()
            if section_bare == bare:
                return idx, s

    return None
