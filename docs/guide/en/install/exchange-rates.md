---
description: Exchange rates in Specivo — how reports convert amounts into the reporting currency, entering rates by hand, importing them from CSV, the optional daily fetch of the ECB reference rates, and running fully offline.
---

# Exchange rates

Money reports add amounts up in one currency, the [reporting currency](currencies.md). Amounts in
other currencies are converted with daily **exchange rates**. Specivo never needs the internet for
this: an administrator enters rates by hand or imports them from a CSV file. Fetching the European
Central Bank (ECB) reference rates every day is an option, and it is **off** until an administrator
switches it on.

Open **Admin → Exchange rates**.

## What a rate means

A rate reads `1 base = rate quote`: `1 USD = 32.45 THB` on 2026-11-05. Rates are daily; a rate
belongs to one date. Specivo stores the pair the way it was entered and derives the other direction
itself (`1 THB = 1 / 32.45 USD`), so you never enter both.

Each rate has a **source**:

| Source | Where it comes from |
|---|---|
| Manual | Entered on the admin page or through the API |
| CSV import | Imported from a file |
| ECB | Fetched from the ECB, or from a feed you point Specivo at |

## How an amount is converted

For an amount in THB dated, say, 2026-11-05, to be reported in USD:

1. **Which date.** Specivo takes the newest rate **on or before** the amount's date, but only if it
   is at most **7 days** old (the *maximum age*, changeable below). Weekends and bank holidays are
   covered that way: Saturday's amounts use Friday's rate.
2. **Which pair.** It looks for the pair itself (THB → USD), or the opposite direction
   (USD → THB, used as `1 / rate`). If neither has a usable rate, it goes through the **pivot
   currency** (EUR by default): THB → EUR → USD. Each leg follows the same rules.
3. **Which source.** The newest date wins. On the same date a manual rate wins over an imported
   one, which wins over a fetched one. So to correct a fetched rate, add a manual rate for that day.

If no rate is found, the amount is **not converted**. Reports list such amounts separately, in their
own currency and with the reason, next to the total: "no rate" (nothing on or before that date) or
"stale" (the newest rate is older than the maximum age). They are never dropped and never counted
as zero.

A report may also ask for the **latest** rate instead (the newest rate there is, whatever its date),
or for the rates of one fixed date for every amount. Reports show which rate dates they used.

Rates and amounts are exact decimal numbers, never floating point. A rate keeps up to 12 decimal
places. A converted amount keeps 8 decimal places and is rounded to the currency's own decimal
places only when shown.

### Try a conversion

The **Try a conversion** card converts an amount on a date with the stored rates and shows the rate
it used, its date and whether it went through the inverse or the pivot. When it cannot convert, it
says why.

## Enter a rate by hand

In **Add a rate**, pick the date, the two currencies and type the rate, with a dot as the decimal
separator (`36.5123`). Both currencies must be enabled on the [Currencies](currencies.md) page. There
is one manual rate per date and pair; to change it, click **Edit** in the list.

Only manual rates can be edited. Imported and fetched rates can be deleted, and a manual rate for
the same day overrides them. A deleted fetched rate comes back with the next fetch of that day.

## Import a CSV file

In **Import a CSV file**, upload a file or paste lines, then click **Preview**. The preview lists
what the import would do with each line (new, changes the imported rate, unchanged) and every
problem with its line number. Nothing is written until you click **Import**.

```csv
date,base,quote,rate
2026-11-04,USD,THB,32.40
2026-11-05,USD,THB,32.45
2026-11-05,EUR,THB,37.533
```

- One rate per line: `date,base,quote,rate`. The header line is optional; blank lines and lines
  starting with `#` are skipped.
- Dates are `YYYY-MM-DD`. Codes are ISO 4217 codes of enabled currencies, in any case. Rates are
  positive, with a dot and at most 12 decimal places, without thousands separators.
- A date and pair may appear only once in a file.
- **All or nothing:** if any line has a problem, nothing is imported. Fix the file and import it
  again.
- Importing the same date and pair again replaces the imported rate; manual and fetched rates are
  left alone. Re-importing a file is safe.
- Up to 2 MB and 20,000 lines per file. For a longer history, split it.

Spreadsheets can export this format directly. The ECB publishes its full history as CSV and XML,
which you can turn into this format to backfill past years.

## Fetch the ECB rates automatically (optional)

The fetch is **off by default**. Nothing is requested from the internet until you switch it on.

In **Conversion settings**, tick **Fetch the rates once a day** and click **Save**. Every day at
16:15 UTC, after the ECB publishes (around 16:00 CET on working days), the Celery worker downloads
the [ECB euro reference rates](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html)
(about 30 currencies against the euro, including USD, JPY, GBP, CNY, INR and THB) and stores them
with source *ECB*. A rate already stored for that day is updated. **Fetch now** runs it at once,
also when the daily fetch is off, which is a quick way to test the connection.

The card shows the outcome of the last fetch. A failed fetch is not retried until the next day,
and the 7-day maximum age covers a few missed days.

**Network.** The worker (`celery-worker`) makes the daily request, and the `api` container makes
the **Fetch now** request. Both need outbound HTTPS to `www.ecb.europa.eu`. Behind a proxy, set
`HTTPS_PROXY` (and `NO_PROXY` for internal hosts) for both containers, for example in `.env`, which
the bundled Compose file passes to each of them; the fetch honours them, like the model download for
[semantic search](semantic-search.md). Without network access the fetch
fails with "Could not reach the feed" and nothing else is affected.

**Feed address.** Leave **Feed address** empty for the ECB daily file. You can point it at:

- another ECB file, such as `https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml`
  (the last 90 days, handy once to fill a gap);
- a self-hosted [Frankfurter](https://frankfurter.dev) instance (MIT licence), which serves the
  same ECB data as JSON: `/v1/latest` or `/v2/rates?providers=ecb` on your server.

Specivo reads the ECB XML and the Frankfurter v1 and v2 JSON formats, in UTF-8. The address must be
`http://` or `https://`.

**Which addresses the fetch may reach.** Every request, and every redirect (at most three), is
checked where it really goes: the host name is resolved and each address it resolves to is checked,
and the connection is made to the checked address.

- Link-local and cloud metadata addresses (such as `169.254.169.254`) are always refused.
- Loopback and private addresses (`127.0.0.1`, `10.x`, `172.16-31.x`, `192.168.x`, `fd00::/8`) are
  refused too, unless the operator sets `EXCHANGE_RATE_FEED_ALLOW_PRIVATE_NETWORKS=true` in `.env`.
  Set it when your Frankfurter instance runs on your internal network. It is an environment setting
  on purpose: the admin page cannot open internal addresses.
- Numeric host spellings other than an ordinary address (`0x7f.1`, `2130706433`) are refused.
- Behind a proxy (`HTTPS_PROXY`), the proxy is chosen again for every redirect, and the target host
  is resolved and checked on the server before the request goes to the proxy. A host that does not
  resolve on the server is **refused**, because it cannot be checked. If your server can only
  resolve names through the proxy, set `EXCHANGE_RATE_FEED_ALLOW_UNRESOLVED_VIA_PROXY=true` in
  `.env`.

!!! warning "With a proxy, the proxy must enforce egress rules"
    The proxy resolves the host name again by itself, so Specivo's check cannot stop a name that
    answers differently the second time (DNS rebinding), and with
    `EXCHANGE_RATE_FEED_ALLOW_UNRESOLVED_VIA_PROXY` nothing is checked on the server at all. When a
    proxy is configured, it must refuse connections to private and link-local ranges
    (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`, `169.254.0.0/16`, `fc00::/7`,
    `fe80::/10`) unless you mean to allow them.

- The whole fetch, download included, must finish within 20 seconds, and the file may be at most
  8 MB.

Error messages on the admin page name what went wrong ("the feed returned an error", "invalid rate
in item 3") but never repeat what the server sent.

### Terms of use of the ECB rates

The ECB publishes the euro reference rates **for information purposes only** and strongly
discourages using them for transactions. They suit cost and spend reporting, which is what
Specivo uses them for, not invoicing or payments.

The [ECB's copyright terms](https://www.ecb.europa.eu/services/disclaimer/html/index.en.html)
allow free use of information from its website on condition that:

- the ECB is cited as the source and the information is reproduced accurately — the admin page
  names the ECB as the source of fetched rates;
- changes to the information are stated — Specivo stores the published rates unchanged and only
  derives inverse and cross rates (through the pivot) when converting, which the conversion
  preview and reports state as "inverse" or "through EUR";
- if you resell documents that contain the rates, buyers are told the rates are available free of
  charge.

Frankfurter is MIT-licensed software and does not own the data: the terms of the provider (here
the ECB) apply. These terms were checked in November 2026; if you rely on the rates, review the
ECB page yourself.

## Air-gapped installs

Keep the fetch off. Enter the few rates you need by hand, or import a CSV file regularly, for
example monthly. To get ECB rates into an offline install, download
`https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip` on a connected machine, convert the
rows you need to `date,base,quote,rate` (base `EUR`), carry the file across with the rest of your
transfers and import it. If your reports cover more days than the maximum age between imports,
raise the maximum age in **Conversion settings** rather than leaving amounts unconverted.

See also [Air-gapped install](air-gapped.md).

## Conversion settings

| Setting | Default | What it does |
|---|---|---|
| Pivot currency | EUR | Currency used when no rate links two currencies directly. Keep EUR with ECB rates, which are all against the euro. |
| Maximum age of a rate | 7 days | Oldest rate used for an amount; `0` means only a rate of the same date. |
| Fetch the rates once a day | off | The optional daily fetch. |
| Feed address | the ECB daily file | Where the fetch reads from. |

## The API

All endpoints are for administrators. Rates are decimal **strings** (`"32.45"`), never JSON numbers.

| Endpoint | What |
|---|---|
| `GET /api/v1/admin/exchange-rates/` | Rates, newest date first. Filters: `base`, `quote`, `currency` (either side), `source`, `date_from`, `date_to`; `offset`, `limit`. |
| `POST /api/v1/admin/exchange-rates/` | Add a manual rate: `{"rate_date": "2026-11-05", "base": "USD", "quote": "THB", "rate": "32.45"}`. `409 exchange_rate_exists` for a second one on the same date and pair. |
| `GET` / `PATCH` / `DELETE /api/v1/admin/exchange-rates/{id}/` | Read, edit (manual rates only, `422 exchange_rate_not_editable` otherwise) or delete a rate. |
| `POST /api/v1/admin/exchange-rates/import/` | `{"csv": "...", "dry_run": true}`. Returns counts, a preview and every error with its line; `422` when there are errors and it was not a dry run. |
| `GET` / `PATCH /api/v1/admin/exchange-rates/settings/` | `pivot`, `max_age_days`, `fetch_enabled`, `fetch_url`, and `last_fetch` (read only). |
| `POST /api/v1/admin/exchange-rates/fetch/` | Fetch now. A failure is reported in the body (`ok: false`, `error`). |
| `POST /api/v1/admin/exchange-rates/convert/` | `{"money": {"amount": "100", "currency": "USD"}, "to": "THB", "on": "2026-11-05", "policy": "entry_date"}`. `policy` is `entry_date` (default), `latest` or a date. Returns `status: converted` with the amount and the rate used, or `status: unconverted` with `reason` `no_rate` or `stale`. |

The generic settings endpoint (`PATCH /api/v1/admin/settings/`) refuses the exchange rate settings
keys; use the endpoint above.

Adding, editing, deleting and importing rates, switching the fetch on or off and changing the
settings are written to the [security audit log](audit-log.md#exchange-rates).
