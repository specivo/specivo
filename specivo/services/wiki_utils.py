"""Wiki utility functions — shared helpers for wiki services."""

from __future__ import annotations

import re


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
