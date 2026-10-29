"""Natural ("human") ordering for free-text names such as version numbers.

Plain string comparison puts ``"0.10.0"`` before ``"0.2.0"`` and ``"Sprint 10"``
before ``"Sprint 9"``. :func:`natural_sort_key` splits a name into runs of digits
and runs of everything else, and compares digit runs as integers, so
``"0.2.0" < "0.10.0" < "0.11.0"`` and ``"Sprint 9" < "Sprint 10"``.

The key is computed in Python, so the result does not depend on the database
collation.
"""

from __future__ import annotations

import re

_DIGIT_RUN = re.compile(r"(\d+)")


def natural_sort_key(value: str) -> tuple[tuple[str | int, ...], str]:
    """Return a sort key that orders *value* naturally.

    Text runs compare case-insensitively; digit runs compare by numeric value.
    ``re.split`` with a capturing group always alternates text and digit runs
    (text at even positions, possibly empty), so two keys never compare a
    string with an integer.

    A name that is a prefix of another sorts first: ``"v1.2"`` comes before
    ``"v1.2-rc1"``, which comes before ``"v1.2-rc2"``, ``"v1.2-rc10"`` and
    ``"v1.2.1"``. Names that differ only in case or leading zeros
    (``"V1"``/``"v1"``, ``"1.01"``/``"1.1"``) fall back to the original string,
    so the order is total and stable.
    """
    parts = _DIGIT_RUN.split(value.casefold())
    key = tuple(int(part) if index % 2 else part for index, part in enumerate(parts))
    return key, value
