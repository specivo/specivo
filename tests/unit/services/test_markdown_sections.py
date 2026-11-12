"""Splitting Markdown into heading sections leaves fenced code alone."""

from __future__ import annotations

from specivo.services.markdown_sections import find_section, split_into_sections

PAGE = """Intro
## Install
```bash
# install deps
pip install specivo
```
~~~
## not a heading either
~~~
## Usage
Run it.
"""


def test_hash_lines_inside_code_fences_are_not_headings() -> None:
    headings = [s["heading"] for s in split_into_sections(PAGE)]
    assert headings == [None, "## Install", "## Usage"]


def test_a_section_keeps_its_whole_code_block() -> None:
    sections = split_into_sections(PAGE)
    _idx, install = find_section(sections, "Install")
    assert "# install deps" in install["body"] and install["body"].count("```") == 2
    assert "## not a heading either" in install["body"]


def test_a_longer_fence_is_not_closed_by_a_shorter_one() -> None:
    text = "## A\n````\n```\n# still code\n````\n## B\n"
    assert [s["heading"] for s in split_into_sections(text)] == [None, "## A", "## B"]
