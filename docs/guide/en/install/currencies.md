---
description: Currencies in Specivo — the ISO 4217 list, enabling and disabling currencies for pickers, the instance reporting currency and the per-project override, and how amounts are stored and rounded.
---

# Currencies and the reporting currency

Specivo stores every amount of money as an exact decimal number together with its currency. The
currencies come from the ISO 4217 standard, and an administrator decides which of them people can
pick and which one money reports use.

## The currency list

Open **Admin → Currencies**. The table lists every current ISO 4217 code with its name and symbol
in your language, its number of decimal places, its kind and whether it is enabled.

| Kind | What it is | Enabled at first |
|---|---|---|
| Currency | Money a country or region uses, such as USD, EUR, THB or JPY | Yes |
| Fund | Fund codes such as CLF (Unidad de Fomento) or USN (US dollar, next day) | No |
| Precious metal | Gold, silver, platinum and palladium (XAU, XAG, XPT, XPD) | No |
| Special code | Units of account such as XDR, and XXX ("no currency") | No |
| Testing code | XTS, reserved for tests | No |

Search by code or name, or show only enabled or only disabled currencies, with the filter above the
table. **Disable** hides a currency from every currency picker; **Enable** brings it back.

Disabling a currency never breaks anything that already uses it. Amounts stay as they are, a
project that reports in it keeps doing so, and saving a form that already holds it still works. It
just cannot be chosen anew.

## Reporting currency

Money reports add up amounts in one currency, the **reporting currency**. It is **USD** on a new
instance. To change it, pick another currency in the **Reporting currency** card at the top of
**Admin → Currencies** and click **Save**. Only enabled currencies are offered.

A project can use a different one. On the project's **Settings → General** tab, a project manager
picks a currency under **Reporting currency**, or leaves **Instance default** to follow the
instance setting (see [Projects](../projects/index.md#reporting-currency)).

Changing a reporting currency does not change any stored amount. It only decides which currency
reports convert to.

## How amounts are stored and rounded

- Amounts are exact decimal numbers, never floating point.
- An amount someone types, such as a price or a fee, may not have more decimal places than its
  currency allows: two for USD or EUR, none for JPY, three for BHD. `10.50` USD is accepted,
  `10.505` USD is refused.
- Amounts Specivo computes, such as the cost of a number of AI tokens, are stored with full
  precision (up to eight decimal places) and rounded only when shown or totalled. Rounding is half
  up: `10.005` USD is shown as `$10.01`.
- The API sends and receives amounts as strings (`"12.50"`), never as JSON numbers.

## The API

| Endpoint | Who | What |
|---|---|---|
| `GET /api/v1/currencies/` | any signed-in user | Enabled currencies: code, name and symbol in the reader's language, decimal places. `?include=EUR` adds that code even when it is disabled. |
| `GET /api/v1/admin/currencies/` | administrators | Every currency with its ISO name, numeric code, kind and `is_active`. `?active=true` or `false` filters. |
| `PATCH /api/v1/admin/currencies/{code}/` | administrators | `{"is_active": false}` disables a currency, `true` enables it. |
| `GET` / `PATCH /api/v1/admin/settings/reporting-currency/` | administrators | Read or set the instance reporting currency: `{"code": "EUR"}`. |
| `PATCH /api/v1/projects/{key}/` | project managers | `{"reporting_currency_code": "THB"}` sets a project's own reporting currency; `null` clears it. |

A code that is not an enabled currency is refused with `422` and the error code `currency_unknown`
or `currency_inactive`. The generic settings endpoint (`PATCH /api/v1/admin/settings/`) refuses the
`reporting_currency` key; use the endpoint above.

Switching a currency on or off and every change of a reporting currency are written to the
[security audit log](audit-log.md#currencies).

## Where the list comes from

The list ships with Specivo in `specivo/data/iso4217.csv`. ISO 4217 changes a few times a year;
new releases of Specivo carry those changes. Your choices of enabled currencies are kept across
upgrades, and a code withdrawn from the standard is disabled, never removed.
