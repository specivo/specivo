"""Cross-check the committed ISO 4217 list (``specivo/data/iso4217.csv``) against Babel's CLDR data.

The list is the source of truth for codes and minor units; Babel is an
independent second source. Where the two legitimately disagree the difference
is listed below with its reason, so a refresh that introduces a new
disagreement fails here and gets looked at.
"""

from __future__ import annotations

import csv

import pytest
from babel.core import get_global
from babel.numbers import get_currency_name, get_currency_precision, list_currencies

from specivo.core.currency_data import (
    CURRENCY_KINDS,
    DEFAULT_REPORTING_CURRENCY,
    ISO4217_CSV,
    iso4217_by_code,
    load_iso4217,
)

pytestmark = pytest.mark.unit

# Tender in CLDR (Babel 2.18) but withdrawn from ISO 4217.
#   BGN: Bulgaria adopted the euro on 2026-01-01.
BABEL_TENDER_NOT_IN_ISO = frozenset({"BGN"})

# In ISO 4217 but not yet in CLDR (Babel 2.18).
#   XAD: Arab Accounting Dinar, a fund code.
ISO_NOT_IN_BABEL = frozenset({"XAD"})

# ISO minor units differ from CLDR's display precision. CLDR shows these
# without decimals because the minor unit is not used in practice; ISO, which
# governs what may be stored and entered, keeps them. Money.format() renders
# the ISO minor units.
MINOR_UNITS_DIFFER_FROM_CLDR = {
    "AFN": (2, 0),
    "ALL": (2, 0),
    "IQD": (3, 0),
    "IRR": (2, 0),
    "KPW": (2, 0),
    "LAK": (2, 0),
    "LBP": (2, 0),
    "MGA": (2, 0),
    "MMK": (2, 0),
    "RSD": (2, 0),
    "SOS": (2, 0),
    "SYP": (2, 0),
    "YER": (2, 0),
}


def _babel_current_tender() -> set[str]:
    """Codes CLDR lists as current legal tender in at least one territory."""
    codes: set[str] = set()
    for entries in get_global("territory_currencies").values():
        for code, _start, end, tender in entries:
            if end is None and tender:
                codes.add(code)
    return codes


@pytest.fixture(scope="module")
def rows():
    return load_iso4217()


def test_file_shape(rows) -> None:
    with ISO4217_CSV.open(encoding="utf-8", newline="") as fh:
        header = next(csv.reader(fh))
    assert header == ["code", "numeric_code", "name", "minor_units", "symbol", "kind", "is_active"]
    codes = [r.code for r in rows]
    assert codes == sorted(codes), "keep the file sorted by code"
    assert len(codes) == len(set(codes)), "one row per code"
    numerics = [r.numeric_code for r in rows if r.numeric_code is not None]
    assert len(numerics) == len(set(numerics)), "numeric codes are unique"
    assert {r.kind for r in rows} <= set(CURRENCY_KINDS)
    assert all(r.name and len(r.name) <= 100 for r in rows)
    assert all(r.symbol is None or len(r.symbol) <= 8 for r in rows)
    assert all(r.minor_units is None or 0 <= r.minor_units <= 4 for r in rows)


def test_only_national_currencies_are_seeded_active(rows) -> None:
    for r in rows:
        assert r.is_active == (r.kind == "national"), r.code


def test_well_known_rows(rows) -> None:
    by_code = iso4217_by_code()
    assert by_code["USD"].numeric_code == 840 and by_code["USD"].minor_units == 2
    assert by_code["THB"].name == "Baht" and by_code["THB"].kind == "national"
    assert by_code["JPY"].minor_units == 0
    assert by_code["BHD"].minor_units == 3
    assert by_code["CLF"].minor_units == 4 and by_code["CLF"].kind == "fund"
    assert by_code["XAU"].minor_units is None and by_code["XAU"].kind == "metal"
    assert by_code["XTS"].kind == "test"
    assert by_code["XXX"].kind == "special" and by_code["XDR"].kind == "special"
    # Regional tender is national, not special.
    assert {by_code[c].kind for c in ("XAF", "XOF", "XCD", "XPF", "EUR")} == {"national"}
    assert DEFAULT_REPORTING_CURRENCY in by_code and by_code[DEFAULT_REPORTING_CURRENCY].is_active


def test_every_current_tender_currency_in_babel_is_in_the_seed(rows) -> None:
    seeded = {r.code for r in rows}
    missing = _babel_current_tender() - seeded - BABEL_TENDER_NOT_IN_ISO
    assert not missing, f"Babel lists current tender missing from iso4217.csv: {sorted(missing)}"


def test_exception_lists_are_not_stale(rows) -> None:
    seeded = {r.code for r in rows}
    assert not BABEL_TENDER_NOT_IN_ISO & seeded, "a code came back into ISO 4217; drop it from the exception list"
    assert BABEL_TENDER_NOT_IN_ISO <= _babel_current_tender(), "Babel caught up; drop the code from the exceptions"
    assert ISO_NOT_IN_BABEL <= seeded
    assert not ISO_NOT_IN_BABEL & set(list_currencies()), "Babel caught up; drop the code from the exceptions"
    for code, (iso, cldr) in MINOR_UNITS_DIFFER_FROM_CLDR.items():
        assert iso4217_by_code()[code].minor_units == iso, code
        assert get_currency_precision(code) == cldr, f"CLDR changed {code}; update or drop the exception"


def test_every_seeded_code_is_known_to_babel(rows) -> None:
    unknown = {r.code for r in rows} - set(list_currencies()) - ISO_NOT_IN_BABEL
    assert not unknown, f"codes Babel does not know: {sorted(unknown)}"


def test_minor_units_match_babel(rows) -> None:
    mismatches = {}
    for r in rows:
        if r.minor_units is None or r.code in MINOR_UNITS_DIFFER_FROM_CLDR:
            continue
        precision = get_currency_precision(r.code)
        if precision != r.minor_units:
            mismatches[r.code] = (r.minor_units, precision)
    assert not mismatches, f"ISO vs CLDR minor units (iso, cldr): {mismatches}"


def test_babel_names_every_active_currency(rows) -> None:
    unnamed = [r.code for r in rows if r.is_active and get_currency_name(r.code, locale="en") == r.code]
    assert not unnamed
