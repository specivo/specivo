"""Argument parsing helpers shared by the import command and source adapters.

Used as ``type=`` callbacks, so a malformed value is reported by argparse with
the option's name rather than surfacing later as a stack trace.
"""

from __future__ import annotations

import argparse


def parse_map(value: str | None) -> dict[str, str]:
    """Parse ``a=b,c=d`` into a dict, tolerating spaces around the parts."""
    if not value:
        return {}
    pairs: dict[str, str] = {}
    for item in value.split(","):
        if not item.strip():
            continue
        key, separator, mapped = item.partition("=")
        if not separator:
            raise argparse.ArgumentTypeError(f"Expected key=value, got {item.strip()!r}")
        pairs[key.strip()] = mapped.strip()
    return pairs


def parse_list(value: str | None) -> tuple[str, ...] | None:
    """Parse a comma-separated list, returning None when empty."""
    if not value:
        return None
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    return items or None
