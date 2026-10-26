"""Wiki utility functions — shared helpers for wiki services."""

from __future__ import annotations

import re

# A ``[[Target]]`` or ``[[Target|display text]]`` wiki link. Neither part may
# contain a bracket, so each match attempt stops at the next ``[`` or ``]`` and
# a run of unclosed ``[[`` openers is scanned in linear time. (Letting the
# target contain ``[`` made ``"[[" * 8000`` take seconds.)
WIKI_LINK_RE = re.compile(r"\[\[([^\[\]|]+)(?:\|([^\[\]]+))?\]\]")


def slugify(title: str) -> str:
    """Generate a URL-friendly slug from a title.

    Lowercases, replaces spaces and underscores with hyphens, strips
    non-alphanumeric characters, and collapses consecutive hyphens.
    """
    slug = title.lower().replace(" ", "-").replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    # Collapse consecutive hyphens
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")
