"""The committed ISO 4217 reference list (``specivo/data/iso4217.csv``).

The ``currencies`` table is seeded from this file and kept in step with it by
migrations, so the codes and minor units here are the ones in the database.
Code that needs only those facts (the :class:`specivo.core.money.Money` type,
Pydantic validation) reads them from here without a database round trip; what
can change at runtime (``is_active``) is only ever read from the table.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ISO4217_CSV = Path(__file__).resolve().parent.parent / "data" / "iso4217.csv"

CURRENCY_CODE_RE = re.compile(r"^[A-Z]{3}$")

#: Allowed values of ``currencies.kind`` (``ck_currencies_kind``).
CURRENCY_KINDS = ("national", "fund", "metal", "special", "test")

#: The reporting currency when the instance setting is missing.
DEFAULT_REPORTING_CURRENCY = "USD"


@dataclass(frozen=True, slots=True)
class CurrencyInfo:
    """One row of the ISO 4217 list."""

    code: str
    numeric_code: int | None
    name: str
    minor_units: int | None  # None = ISO "N.A."
    symbol: str | None
    kind: str
    is_active: bool  # the seeded default only


def _parse_row(row: dict[str, str]) -> CurrencyInfo:
    code = row["code"].strip()
    if not CURRENCY_CODE_RE.match(code):
        raise ValueError(f"Invalid currency code in {ISO4217_CSV.name}: {code!r}")
    kind = row["kind"].strip()
    if kind not in CURRENCY_KINDS:
        raise ValueError(f"Invalid kind for {code} in {ISO4217_CSV.name}: {kind!r}")
    numeric = row["numeric_code"].strip()
    minor = row["minor_units"].strip()
    return CurrencyInfo(
        code=code,
        numeric_code=int(numeric) if numeric else None,
        name=row["name"].strip(),
        minor_units=int(minor) if minor else None,
        symbol=row["symbol"].strip() or None,
        kind=kind,
        is_active=row["is_active"].strip().lower() == "true",
    )


def load_iso4217(path: Path = ISO4217_CSV) -> list[CurrencyInfo]:
    """Parse the CSV at *path* into rows, in file order."""
    with path.open(encoding="utf-8", newline="") as fh:
        return [_parse_row(row) for row in csv.DictReader(fh)]


@lru_cache(maxsize=1)
def iso4217_by_code() -> dict[str, CurrencyInfo]:
    """The committed list keyed by code (parsed once per process)."""
    return {c.code: c for c in load_iso4217()}


def is_known_currency(code: str) -> bool:
    """Whether *code* is in the committed ISO 4217 list."""
    return code in iso4217_by_code()


def minor_units_for(code: str) -> int | None:
    """ISO minor units of *code*; ``None`` for "N.A." codes.

    Raises :class:`KeyError` for a code that is not in the list.
    """
    return iso4217_by_code()[code].minor_units
