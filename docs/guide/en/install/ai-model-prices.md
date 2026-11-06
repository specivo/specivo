---
description: AI model prices in Specivo — per-token prices with cache and long-context rates, kept as a history so usage is valued at the price of its own time, bundled from LiteLLM and overridable in any currency.
---

# AI model prices

Specivo values AI usage at **API prices**: what the same tokens would have cost on a provider's
pay-as-you-go price list. The prices live in a table administrators manage under
**Admin → AI model prices**. Each version of Specivo ships the list prices of several hundred models,
and you can add your own, in any enabled [currency](currencies.md).

Usage covered by a flat-rate plan such as Claude Max is still valued at these prices, to compare it
with the plan's fee; record the plans under [AI subscriptions](ai-subscriptions.md).

Prices change over time, so the table is a **history**. Every price is valid for a period, and usage
is always priced with the price that was valid at its own time. Changing a price never rewrites an
old one.

## What a price holds

All prices are **per one million tokens**, in one currency. Usage is counted in token classes:

| Price | Applies to |
|---|---|
| Input | Fresh input tokens, not counting cache reads and writes |
| Output | Generated tokens |
| Cache write, 5 minutes | Prompt cache writes kept for five minutes |
| Cache write, 1 hour | Prompt cache writes kept for an hour. When a provider publishes none, the bundled price is twice the input price. |
| Cache read | Prompt cache hits |
| Reasoning | Reasoning ("thinking") tokens reported apart from output. Empty means they cost the same as output. |

Any other empty price means that kind of token is **not priced**. Usage with such tokens is shown
as *partly priced*, never as if those tokens were free. A price of `0` is a real price: free.

### Long context

Some providers charge more once a request carries a long context, for example above 200,000 tokens.
A price can hold a **threshold** and a second set of prices for that tier. A request whose context
(input plus cache reads and writes) is above the threshold is billed **entirely** at the long
context prices; exactly at the threshold it is not. A long context price left empty falls back to
the normal price of the same kind.

### Local models

A price can be marked as a **local model**, with an optional **baseline model**: the paid model whose
prices value the local model's usage, to show what running it locally saved.

## Where prices come from

Each price has a **source**:

| Source | Where it comes from |
|---|---|
| Bundled | The price list shipped with this version of Specivo |
| Admin | Entered by an administrator on the page or through the API |

Where a bundled price and one you entered are both valid, **yours wins**.

### The bundled prices

The bundled list comes from [LiteLLM](https://github.com/BerriAI/litellm)'s model price list (MIT
License), trimmed to the text models of the providers coding agents use directly: Anthropic,
OpenAI, Google Gemini, xAI, DeepSeek, Mistral, Moonshot, Z.ai and Alibaba DashScope. The card
**Bundled prices** shows how many models this version carries and the date the list describes.

LiteLLM publishes **current** prices only, without their history. So the bundled list is loaded
**append-only**:

- A model that is new to your instance gets a price valid **from the list's date**.
- An unchanged price changes nothing.
- A changed price **ends** the bundled period at the new list's date and starts a new one. The old
  period stays, so older usage keeps its old price.
- Prices you entered are never touched, and neither is a bundled price you deactivated.

The list is loaded when Specivo is installed, and again whenever a worker starts, so an upgrade that
brings a newer list applies it by itself. **Load bundled prices now** does the same on demand.

Usage from **before** the first bundled period of a model is not priced: Specivo never falls back
to a later price for older usage. If you need prices for that time, enter them as a past period
(below).

## Find a price

The list shows by default the prices **valid now**, one line per model and source. Filter by
provider, by part of a model name or alias, by source, or by status (active or deactivated), and
switch to **All periods** to see past ones too. Click a model to open its **price history**: every
period, bundled and yours, newest first. The period in use right now is marked **In use now**.

### How a model name is matched

Tools report model names in different shapes. Before matching, Specivo:

1. lowers the case and trims spaces;
2. drops provider prefixes such as `anthropic/`, `openai/`, `google/` or `models/`
   (`Anthropic/Claude-Sonnet-4-5` becomes `claude-sonnet-4-5`);
3. looks for a model of that name, then for a model that lists it as an **alias**.

Among the prices valid at the usage's time, an exact model name beats an alias, and a price you
entered beats a bundled one. If nothing is valid then, the usage is **unpriced**.

## Change a price

Prices are never edited in place. A price change is a **new price from an instant**:

1. Open the model from the list.
2. Under **New price from…**, the form is filled with the current prices. Change what changed.
3. Set **Valid from (UTC)** to the instant the new price takes effect, and save.

The price you entered before for that model now **ends** at that instant, and the new one starts
there. The two periods meet exactly: usage at the change instant gets the new price, usage a moment
before gets the old one. All times are UTC.

To add a price for a model that has none, or to override a bundled price, use **Add a price** on the
list page and type the provider and model.

### Enter a past period

When a provider announces a price change late, or you need prices for usage from before the bundled
list's date, enter a **past period**: fill in **Valid until (UTC)** as well. A past period must not
overlap another price you entered for the same model; end or deactivate that one first.

### Other currencies

Pick any enabled currency for a price, for example CNY for a provider that bills in yuan, or EUR or
THB for a contract price. Usage is then valued in that currency, and reports convert it with the
[exchange rates](exchange-rates.md) like any other amount.

## End, deactivate or delete a price

Under **Details** on a period of the price history:

- **Save end** sets or clears the end of the period. Clearing it reopens the period.
- **Deactivate** takes the price out of use without deleting it; **Activate** puts it back.
- **Delete** removes a price you entered. Bundled prices cannot be deleted (they would come back on
  the next load); deactivate them instead.
- **Aliases** and the **note** (for example a link to the provider's announcement) can be edited at
  any time.

Once usage has been priced with a price, that price **cannot be deleted**, and its end cannot be
moved to before that usage. Deactivate it instead. Neither its amounts nor its start can ever
change: enter a new price.

## Through the API

Everything on the page is available to administrators under `/api/v1/admin/ai-model-prices/`.
Prices are decimal **strings** such as `"3.75"`,
never JSON numbers, and instants need a time zone offset (`2026-06-01T00:00:00Z`).

```bash
# Prices valid at an instant (given here in Asia/Bangkok time)
curl -H "Authorization: Bearer $TOKEN" \
  "https://specivo.example.com/api/v1/admin/ai-model-prices/resolve/?model=claude-sonnet-4-5&at=2026-11-06T09:00:00%2B07:00"

# A new price from an instant: ends your previous price of the model there
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://specivo.example.com/api/v1/admin/ai-model-prices/ \
  -d '{"provider": "anthropic", "model": "claude-sonnet-4-5", "currency": "USD",
       "prices": {"input": "3", "output": "15", "cache_write_5m": "3.75",
                  "cache_write_1h": "6", "cache_read": "0.3"},
       "effective_from": "2026-12-01T00:00:00Z"}'
```

`resolve/` answers `priced` with the price, or `unpriced`. Other endpoints: `GET /` (list and
filter), `GET /history/?provider=&model=`, `GET /providers/`, `GET`, `PATCH` and `DELETE /{id}/`,
and `POST /seed/` to load the bundled list.

Every change is written to the [audit log](audit-log.md#ai-model-prices).

## Air-gapped installs

The bundled list is part of the image; nothing is downloaded. Enter or correct prices by hand as
needed.
