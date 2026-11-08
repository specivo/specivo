---
description: AI subscriptions in Specivo — record the flat-rate AI plans people pay for (Claude Pro or Max, ChatGPT Plus or Pro, Cursor, Copilot, Google AI), in any currency, so AI usage counts as covered and spend reports can spread the fee.
---

# AI subscriptions

Many people pay for AI with a **flat-rate plan** rather than per token: Claude Pro or Max, ChatGPT
Plus or Pro, Cursor Pro, GitHub Copilot, Google AI Pro. Specivo keeps a register of these personal
plans ("seats"). It is used for two things:

- AI usage from a tool a plan covers is counted as **covered by the subscription**, instead of as
  pay-as-you-go spend or as unknown.
- [Spend reports](../projects/ai-spend-reports.md) spread each plan's fee over the usage it covered, valued at
  [API prices](ai-model-prices.md), so you can compare what the seat costs with what the same usage
  would have cost per token.

Subscriptions are personal: each one belongs to one person. Plans shared by a team are not
supported yet.

## Who sees and changes them

| | Sees | Adds, changes, deletes |
|---|---|---|
| The person | their own | their own, while self-service is on (the default) |
| Administrators | everyone's | everyone's |
| Anyone else, project managers included | nothing | nothing |

Service accounts do not hold subscriptions: usage an agent reports on behalf of a person is matched
to that person's plans.

Administrators can switch **self-service** off under **Admin → AI subscriptions → Who can change
subscriptions**. People then still see their own subscriptions, but only administrators change
them. When an administrator adds, changes or deletes **somebody else's** subscription, the change is
written to the [audit log](audit-log.md#ai-subscriptions).

## Add your subscription

Open **My AI subscriptions** from your user menu or your [profile](../getting-started/your-profile.md).

1. Under **Start from a plan**, choose your plan and click **Fill in**. The form is filled with the
   plan's provider, the tools it covers and a suggested price.
2. Correct the **fee** and **currency** to what you actually pay, for example `3500` THB or `90` EUR
   a month. Choose **Yearly** if you pay once a year.
3. Set the **first day**. Leave **last day** empty while the plan runs; set it when you cancel.
4. Click **Add subscription**.

!!! note "Suggested prices are hints"
    The prices of the listed plans are the providers' published US list prices when this version of
    Specivo was made. They may be out of date, and they leave out tax and local pricing. Always
    enter what you are actually charged.

The listed plans are Claude Pro, Claude Max 5x, Claude Max 20x, ChatGPT Plus, ChatGPT Pro, Cursor
Pro, GitHub Copilot Pro, Pro+ and Max, Google AI Pro, Google AI Ultra (5x and 20x), and SuperGrok. Choose **Custom** for anything else and type
the plan name, the provider and the fee.

The fee is entered with no more decimal places than the currency has (two for USD or THB, none for
JPY), and must be more than zero. Any enabled [currency](currencies.md) can be used.

To change or end a subscription, click **Edit**. To record that you cancelled, set the **last day**
rather than deleting it: deleting removes the plan from past spend reports too.

## Which usage a subscription covers

Each subscription names a **provider** and, optionally, the **tools** it covers.

- **Tools ticked**: the plan covers usage from those tools, whatever model they use. Cursor Pro
  covers Cursor even when Cursor runs a Claude or GPT model.
- **No tools ticked**: the plan covers the tools **of its provider**, the tools whose default
  provider is the plan's, when they use that provider's models. Claude Pro covers Claude Code;
  ChatGPT Plus covers Codex; Google AI Pro covers Gemini CLI. It does not cover another vendor's
  tool that happens to call the same models.

When a person has several plans that could cover the same usage, a plan that lists the tool wins
over a plan that covers the provider's tools; between two of the same kind, the one that started
last wins.

### Days and time zones

The first and last day are **UTC days**, and both count. A plan from 1 March to 31 March covers
usage from 1 March 00:00 UTC up to, but not including, 1 April 00:00 UTC. Usage is matched by the
UTC day of its time, so usage at 06:30 on 1 April in Bangkok (23:30 on 31 March UTC) is still
covered. This is the same day boundary AI usage is priced with.

### How the fee is spread over months

Reports count a plan's fee per calendar month:

- A monthly plan counts its whole fee in every month it covers entirely.
- A yearly plan counts a twelfth of its fee per month.
- A month the plan covers only partly, because it started or ended inside it, counts the share of
  its days: a 20 USD plan that starts on 21 April counts 10/30 of 20, about 6.67 USD, in April.

Amounts stay in the plan's currency; reports convert them with the
[exchange rates](exchange-rates.md).

## AI tools

The tools that report AI usage are listed under **Admin → AI subscriptions → AI tools**. Specivo
starts with Claude Code, Codex, Gemini CLI, Cursor, GitHub Copilot, OpenCode and Other.

| Field | Meaning |
|---|---|
| Slug | The name tools and reporters send, such as `claude-code`. Fixed once created. |
| Name | What people see. |
| Default provider | The model provider assumed for the tool's usage when none is reported, and the provider whose plans cover the tool without listing it. Empty for tools that use several providers, such as Cursor. |
| Position | Order in lists. |
| Active | Inactive tools are left out of pickers. |

When a usage reporter sends a tool Specivo does not know yet, the tool is **added automatically,
inactive**, so new reporters work without an upgrade. Activate it, and give it a name and a default
provider, to offer it in pickers. Tools are never deleted.

## With your AI agent

An agent connected to Specivo over [MCP](../ai-agents/connecting.md) can register your subscriptions
for you. Give it your invoices and ask, for example:

> Here are my Claude and ChatGPT invoices for this year. Register them as my AI subscriptions.

The agent uses the `specivo_ai_subscriptions` tool:

| Operation | What it does |
|---|---|
| `presets` | Lists the plans with their suggested prices, so the agent can match each invoice to a plan. |
| `list` | Shows your subscriptions. Administrators can name another person (`user`). |
| `add` | Registers one or more periods in one call, such as every plan change on your invoices. Each period has a plan (or a custom plan with its provider and name), the fee, the currency, the billing period, the first and last day, and notes. |
| `update` | Corrects one subscription. |
| `end` | Sets the last day of one subscription, today by default. |
| `delete` | Deletes one subscription. Prefer `end`: deleting removes the plan from past reports. |

Register each invoice period as its own subscription, with the amount actually charged, tax
included, in its currency, and a last day on every period that was followed by another. For
example, Claude Max 5x for 100 USD from 21 January to 25 February, then Max 20x for 200 USD from
26 March to 25 May, then Max 5x again.

`add` is all or nothing. When any period is wrong (an unknown plan, a fee that is not a number, a
last day before the first), nothing is added and the agent is told what to fix in each period.

Every answer shows each subscription's plan, fee, currency, days and the tools it covers, and
**warns** when two periods of the same plan family overlap or leave a gap. A plan family is your
plans of the same provider covering the same tools, so Claude Pro, Max 5x and Max 20x are one
family and ChatGPT Plus is another. In the example above, the agent would warn about the gap from
26 February to 25 March. Warnings never block anything, because a gap is often real (a month you
did not pay for) and so is an overlap (an upgrade during a billing period).

The tool follows the same rules as the page: it acts for the person whose API key the agent uses,
writing needs self-service unless that person is an administrator, and an administrator's changes
to somebody else's subscriptions are audited. Two kinds of key are refused, each with a message
saying why:

- **A key limited to some projects.** Subscriptions belong to the account, so use an unlimited
  personal key.
- **A service account's key.** Subscriptions belong to people. Connect the agent with your own
  personal key, or ask an administrator to record them.

Every change the tool makes is also written to the [audit log](audit-log.md#ai-subscriptions) as
`ai_subscriptions_changed`, like every other change an agent makes.

## Through the API

Amounts are decimal **strings** such as `"20.00"`, never JSON numbers; days are `YYYY-MM-DD`.

```bash
# Add your own subscription from a plan, with your own price
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://specivo.example.com/api/v1/my/ai-subscriptions/ \
  -d '{"plan_key": "claude-max-5x", "amount": "3500", "currency": "THB", "starts_on": "2026-11-01"}'

# Record that it ended
curl -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://specivo.example.com/api/v1/my/ai-subscriptions/42/ -d '{"ends_on": "2027-01-31"}'
```

| Endpoint | Who |
|---|---|
| `GET /api/v1/ai-tools/` (`?include_inactive=true`) | anyone signed in |
| `GET /api/v1/ai-subscription-presets/` | anyone signed in |
| `GET`, `POST /api/v1/my/ai-subscriptions/`; `GET`, `PATCH`, `DELETE /api/v1/my/ai-subscriptions/{id}/` | the person, for their own; writing needs self-service |
| `GET`, `POST /api/v1/admin/ai-subscriptions/` (`?user_id=`, `?provider=`, `?active_on=`); `GET`, `PATCH`, `DELETE /api/v1/admin/ai-subscriptions/{id}/` | administrators |
| `GET`, `PATCH /api/v1/admin/ai-subscriptions/settings/` (`{"self_service": false}`) | administrators |
| `GET`, `POST /api/v1/admin/ai-tools/`; `PATCH /api/v1/admin/ai-tools/{slug}/` | administrators |

With a plan's `plan_key`, the fields you leave out take the plan's values; with `custom`, give
`provider`, `plan_name`, `amount` and `currency`. An API key limited to some projects cannot use
these endpoints: subscriptions belong to the account, not to a project.
