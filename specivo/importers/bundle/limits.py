"""Size limits for reading a bundle that comes from outside.

A bundle is untrusted input: a zip can expand to far more than it occupies (a
"zip bomb"), and a single JSON line or JSON file can be large enough to exhaust
memory when it is parsed. Each limit has a default generous enough for a large
real migration and can be raised on the command line when one is not.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB

# A small entry can compress extremely well without being an attack (a file of
# zeros, an empty JSON-lines file), so the ratio is only checked past this size.
RATIO_CHECKED_FROM = 1 * MIB

_SIZE_RE = re.compile(r"^\s*(\d+)\s*([KMGT]?)i?B?\s*$", re.IGNORECASE)
_UNITS = {"": 1, "K": KIB, "M": MIB, "G": GIB, "T": 1024 * GIB}


@dataclass(frozen=True, slots=True)
class BundleLimits:
    """How much of a bundle the reader accepts before it refuses the bundle.

    ``max_unpacked_bytes``, ``max_entries``, ``max_entry_bytes`` and
    ``max_compression_ratio`` apply to a zip, and are counted on the bytes
    actually unpacked, not on what the archive's headers claim.
    ``max_line_bytes`` (one JSON-lines record, newline included) and
    ``max_json_bytes`` (``manifest.json``, ``lookups.json``,
    ``public_access.json``) apply to every bundle.
    """

    max_unpacked_bytes: int = 64 * GIB
    max_entries: int = 1_000_000
    max_entry_bytes: int = 16 * GIB
    max_compression_ratio: int = 200
    max_line_bytes: int = 16 * MIB
    max_json_bytes: int = 64 * MIB


def parse_size(value: str) -> int:
    """Parse a size such as ``512``, ``16M``, ``2G`` or ``1GiB`` into bytes."""
    match = _SIZE_RE.match(value)
    if match is None:
        raise argparse.ArgumentTypeError(f"Expected a size such as 512, 16M or 2G, got {value!r}")
    return int(match.group(1)) * _UNITS[match.group(2).upper()]


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Expected a whole number, got {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"Expected a positive number, got {value!r}")
    return number


def format_size(size: int) -> str:
    """Return *size* in the largest unit it is a whole number of, for messages."""
    for unit in ("T", "G", "M", "K"):
        if size >= _UNITS[unit] and size % _UNITS[unit] == 0:
            return f"{size // _UNITS[unit]}{unit}"
    return str(size)
