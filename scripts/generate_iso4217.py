#!/usr/bin/env python3
"""Regenerate ``specivo/data/iso4217.csv`` from the ISO 4217 list one.

The source is the ``datasets/currency-codes`` data package
(https://github.com/datasets/currency-codes, ``data/codes-all.csv``), which
mirrors the list published by the ISO 4217 maintenance agency. Its maintainers
release their selection and formatting under the Open Data Commons Public
Domain Dedication and License (ODC-PDDL-1.0). See ``specivo/data/README.md``
for the refresh procedure.

Only current codes are kept (rows with a withdrawal date are historic), one row
per alphabetic code. The English symbol comes from Babel's CLDR data and is
left empty where CLDR has none other than the code itself.

Usage::

    uv run python scripts/generate_iso4217.py              # download the source
    uv run python scripts/generate_iso4217.py --source codes-all.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import urllib.request
from pathlib import Path

from babel.numbers import get_currency_symbol

SOURCE_URL = "https://raw.githubusercontent.com/datasets/currency-codes/main/data/codes-all.csv"
OUTPUT = Path(__file__).resolve().parent.parent / "specivo" / "data" / "iso4217.csv"
COLUMNS = ["code", "numeric_code", "name", "minor_units", "symbol", "kind", "is_active"]

# ISO 4217 marks these as funds (``IsFund="true"`` in the agency's XML), which
# the CSV source does not carry.
FUND_CODES = frozenset({"BOV", "CHE", "CHW", "CLF", "COU", "MXV", "USN", "UYI", "UYW", "XAD"})
METAL_CODES = frozenset({"XAG", "XAU", "XPD", "XPT"})
TEST_CODES = frozenset({"XTS"})


def classify(code: str, minor_units: str) -> str:
    """Return the ``kind`` of a current ISO 4217 code."""
    if code in FUND_CODES:
        return "fund"
    if code in METAL_CODES:
        return "metal"
    if code in TEST_CODES:
        return "test"
    # The remaining X codes without minor units are units of account (XDR,
    # XUA, the bond market units, XSU) and XXX. XAF, XOF, XCD, XCG and XPF are
    # real tender and have minor units, so they stay national.
    if code.startswith("X") and not minor_units.isdigit():
        return "special"
    return "national"


def read_source(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    with urllib.request.urlopen(SOURCE_URL, timeout=30) as resp:  # noqa: S310 - fixed https URL
        return str(resp.read().decode("utf-8"))


def build_rows(source_text: str) -> list[dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for rec in csv.DictReader(io.StringIO(source_text)):
        code = (rec.get("AlphabeticCode") or "").strip()
        if not code or (rec.get("WithdrawalDate") or "").strip():
            continue
        if code in rows:
            continue
        minor = (rec.get("MinorUnit") or "").strip()
        minor_units = minor if minor.isdigit() else ""
        symbol = get_currency_symbol(code, locale="en")
        rows[code] = {
            "code": code,
            "numeric_code": str(int(rec["NumericCode"])) if (rec.get("NumericCode") or "").strip() else "",
            "name": rec["Currency"].strip(),
            "minor_units": minor_units,
            "symbol": "" if symbol == code else symbol,
            "kind": classify(code, minor),
            "is_active": "true" if classify(code, minor) == "national" else "false",
        }
    return [rows[c] for c in sorted(rows)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", help="read this codes-all.csv instead of downloading it")
    parser.add_argument("--output", default=str(OUTPUT), help="where to write the CSV")
    args = parser.parse_args(argv)

    rows = build_rows(read_source(args.source))
    if len(rows) < 150:
        print(f"Refusing to write only {len(rows)} codes; is the source complete?", file=sys.stderr)
        return 1
    with open(args.output, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} currencies to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
