# Reference data

## iso4217.csv

The ISO 4217 currency list that seeds the `currencies` table: one row per
current alphabetic code.

| Column | Meaning |
|---|---|
| `code` | Alphabetic code (`USD`, `THB`) |
| `numeric_code` | ISO numeric code, without leading zeros |
| `name` | English ISO name. Localised names come from Babel at render time. |
| `minor_units` | Digits after the decimal point; empty where ISO says "N.A." (metals, units of account, `XXX`) |
| `symbol` | CLDR English symbol from Babel; empty where CLDR has only the code |
| `kind` | `national`, `fund`, `metal`, `special` or `test` |
| `is_active` | Seeded `true` for national currencies only; it filters pickers, nothing else |

**Source.** The `datasets/currency-codes` data package
(<https://github.com/datasets/currency-codes>, file `data/codes-all.csv`), which
mirrors the "list one" published by SIX, the ISO 4217 maintenance agency. The
package maintainers release their selection and formatting under the
[Open Data Commons Public Domain Dedication and License v1.0](https://opendatacommons.org/licenses/pddl/1.0/)
(ODC-PDDL-1.0). The codes, names and minor units themselves are facts of the
standard. SIX publishes the list online free of charge but states no reuse
licence, so the list is not copied from the agency directly. The 2026-09-17
edition of the agency's list and the data package agree on every code, name,
numeric code and minor unit. Symbols come from the Unicode CLDR through Babel
(Unicode License v3).

`kind` is derived by `scripts/generate_iso4217.py`: funds are the codes the
agency marks `IsFund`, metals are `XAG`, `XAU`, `XPD` and `XPT`, `XTS` is the
test code, and the other `X` codes without minor units (units of account and
`XXX`) are special. `XAF`, `XOF`, `XCD`, `XCG` and `XPF` are tender and count
as national.

**Refresh** (the agency publishes amendments a few times a year; check at least
once per release):

1. `uv run python scripts/generate_iso4217.py` downloads the source and
   rewrites `iso4217.csv`. Review the diff: new codes, withdrawn codes and
   changed minor units.
2. If the source added a fund code, add it to `FUND_CODES` in the script.
3. Run `uv run pytest tests/unit/test_currency_data.py`. It cross-checks the
   file against Babel's CLDR data; a disagreement either needs a fix or an
   entry in its documented exception lists.
4. Write an Alembic migration that calls
   `specivo.services.currency_service.sync_currencies_from_csv` (or upserts
   the changed rows itself) so existing installs receive the change. A
   withdrawn code is never deleted, because amounts may reference it; it is
   marked inactive instead.
