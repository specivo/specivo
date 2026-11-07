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

## ai_model_prices.json

The AI model prices bundled with each release: one entry per model, with
prices **per one million tokens** as decimal strings, all in USD. It seeds
the `ai_model_prices` table (rows of source `seed`).

| Field | Meaning |
|---|---|
| `as_of` | The instant the prices describe: the commit time of the source file. New price periods start here. |
| `source` | Source file URL at the pinned commit, its licence and copyright |
| `provider`, `model` | Specivo's provider slug and the normalised model name (lower case, no provider prefix) |
| `input`, `output`, `cache_write_5m`, `cache_write_1h`, `cache_read`, `reasoning` | Prices per 1M tokens. A missing `cache_write_1h` is loaded as twice `input`; a missing `reasoning` means the output price. |
| `long_context_threshold`, `long_*` | The long-context tier: a request whose context exceeds the threshold is billed entirely at these prices |

**Source.** LiteLLM's `model_prices_and_context_window.json`
(<https://github.com/BerriAI/litellm>), MIT License, Copyright (c) 2023 Berri
AI. Only the text models (`chat`, `responses`) of the first-party providers
listed in `specivo.core.ai_pricing_seed.LITELLM_PROVIDERS` are kept, without
batch, flex or priority prices. Per-token prices are multiplied by one million
and rounded half up to six decimal places (the script reports any rounding).
The licence is credited in `THIRD-PARTY-NOTICES.md` and on the admin page.

LiteLLM keeps current prices only. The loader is therefore **append-only**:
an unchanged price is a no-op, a changed price ends the open bundled period at
`as_of` and opens a new one, and administrator prices are never touched.

A model's **first** known period starts at `FIRST_PERIOD_START`
(2000-01-01 UTC, in `specivo.core.ai_pricing_seed`), not at `as_of`, so usage
logged before the snapshot's date is priced with the first known price. A
period counts as first when the model has no earlier bundled period, no
administrator period starting before it, and no period in
`ai_model_prices_history.json`; otherwise a new model's period starts at
`as_of`. Installs whose first periods were stored from an `as_of` by an older
release get them moved back on the next load, and the entries dated before the
old start are re-priced. A history period added later for such a model
overlaps the moved period and is skipped with a warning; enter the older price
as an administrator price instead (admin prices win).

**Refresh** (once per release):

1. `uv run python scripts/refresh_ai_model_prices.py` downloads the newest
   source file, pins the commit that last changed it, and rewrites
   `ai_model_prices.json` with `as_of` set to that commit's time. Use
   `--ref <sha>` for a given commit, or `--source <file> --as-of <instant>`
   offline.
2. Review the diff (one model per line): new models, changed prices, models
   gone. A model that disappears keeps its last price period.
3. Run `uv run pytest tests/unit/test_ai_pricing_seed.py`.
4. Nothing else: workers load the new snapshot when they start after the
   upgrade (`specivo.tasks.ai_model_prices`), and administrators can load it
   from **Admin → AI model prices**.

## ai_model_prices_history.json

Optional, maintained **by hand**: known earlier price periods that the
snapshot cannot carry, such as a provider's price before a cut. Each entry
has the snapshot's model fields plus `effective_from` and `effective_to`
(ISO 8601 with an offset; both required, the end exclusive). Periods of one
model must not overlap each other. The loader inserts a period once, and skips
it (with a warning) when a bundled period already covers part of it. Cite the
provider's announcement in `note`.
