---
description: AI billing accounts in Specivo — record what AI usage is paid through (a company gateway, a company plan, a personal subscription or API key) and who pays, employer or employee, with the share an employer pays back.
---

# AI billing accounts

A **billing account** says what AI usage was paid through and **who pays** for it, the employer or
the employee. It sits beside the cost type, which says how the usage is priced (per token, by
subscription, local or free). Typical accounts:

| Kind | Example | Usually paid by |
|---|---|---|
| Gateway | the company's LiteLLM proxy or other API gateway | the employer |
| Subscription | a company plan, or your own Claude Max or ChatGPT Plus | the employer or the employee |
| API key | your own pay-per-token key used for work | the employee |
| Local model | a model running on your own machine | either |

There are two sorts:

- **Company accounts** (instance accounts) belong to no one. Administrators manage them; everyone
  sees them, so they can name one in their reporter's configuration.
- **Personal accounts** belong to one person. Only that person and administrators see them. Every
  [AI subscription](ai-subscriptions.md) you add gets a personal account of kind *Subscription*,
  which is its fee schedule.

Accounts are never deleted. Switch one off instead: the usage already paid through it keeps
pointing at it, and new usage is no longer matched to it.

## Who sees and changes them

| | Sees | Adds, changes, switches off |
|---|---|---|
| Everyone | company accounts, and their own | their own, while [self-service](ai-subscriptions.md#who-sees-and-changes-them) is on (the default) |
| Administrators | every account | every account |

Service accounts own no billing accounts. Self-service is the same switch as for subscriptions.

## Fields

| Field | Meaning |
|---|---|
| Name | What people see. |
| Slug | What a reporter sends as `billing_account` to name the account, such as `corp-gateway`. Lower case letters, digits, dots, dashes and underscores; unique among company accounts and among each person's own. |
| Kind | Gateway, Subscription, API key or Local model. |
| Who pays | Employer or Employee. |
| Reimbursed share | From 0 to 1, only on accounts the employee pays for: the part of the spend the employer pays back, for example `0.5` for half. |
| Default cost type | How usage through the account is priced when the record does not say: API, Subscription, Local or Free. A gateway is usually *API*. |
| Route patterns | Names of the backend a tool reports (the route, for example the provider id a gateway is configured under in OpenCode or Codex). Usage whose route matches one of them is paid through the account. `*` matches any text; case is ignored. |
| Tools it covers | The tools whose usage the account takes by route. None ticked covers every tool. |

## How usage finds its account

When usage is logged, Specivo picks the account in this order and stops at the first match:

1. An account (or a payer without an account) a person set on the entry by hand, on the
   [entry's edit form](../projects/ai-usage.md#correct-who-paid), with `specivo_ai_usage` or through
   the API. It is kept until someone sets it back to automatic.
2. The account the record names by slug: your own first, then a company account.
3. A company account whose route patterns match the route and that covers the tool.
4. One of your own accounts that matches the same way.
5. The subscription that covers the tool, when the record asks for no cost type or for
   *subscription*.
6. The project's [payer default](../projects/index.md#who-pays-for-ai-usage), with no account.

The account's payer becomes the entry's payer. With none of these, the payer is *unknown*.

Creating an account with route patterns, or changing an account's payer, route patterns, tools,
default cost type or whether it is active, applies the change to the usage already logged that it
affects, and prices that usage again where its cost type changed.

## Company accounts

Open **Admin → AI billing accounts**. Fill in the form under **Add a company account** and click
**Add account**. For a gateway, list the route names its users' tools report under **Route
patterns**, choose **Employer** under **Who pays** and **API** as the default cost type. Click
**Edit** on an account to change it or switch it off. Administrators also open a person's account
from **Admin → AI subscriptions** (the **Billing account** column) to change it.

## Your own accounts

Open **My AI billing accounts** from your user menu or your
[profile](../getting-started/your-profile.md). The page lists your own accounts and, below them,
the company accounts you can name in your reporter's configuration. Add a personal API key you use
for work with **Add an account**, and set the **Reimbursed share** if your employer pays part of it
back. **My AI subscriptions** shows, for each subscription, a link to its account, where you set
its reimbursed share.

## Reimbursement claims

Usage you pay for yourself becomes **reimbursable** when it runs through one of your accounts with
a reimbursed share, in a project whose AI payer default is *employer* (company work). For each such
entry the amount is the share times its seat cost (its part of your subscription's fee for the
month) plus its pay-per-token spend. Usage in your own projects is never reimbursable.

Once a month has ended, claim it:

1. Open **AI reimbursement claims** from your user menu or your
   [profile](../getting-started/your-profile.md), pick the month and select **Create claim**. The
   claim starts as a draft, in the instance's reporting currency, with each entry it covers.
2. Check the entries. **Refresh figures** takes the month's current numbers into the draft, for
   example after late usage was reported or a price was corrected.
3. Select **Submit for approval**. Administrators get an email. From now on the claim's amounts
   never change. **Withdraw** takes a submitted claim back to a draft.
4. An administrator opens **Admin → AI reimbursement claims**, then **Approve** or **Reject**
   (with an optional reason that you see). Once the money is sent, they select **Mark as paid**.
   You get an email for each decision. Nobody decides on their own claim, administrators included.

You have one claim per month. A rejected claim is final: delete it if you like and claim the month
again. **Export CSV** downloads a claim with one line per entry and the total, for your expense
system.

A claim keeps the figures it was made with. When an entry it covers is later edited, re-priced,
moved to another account or project, excluded or deleted, or an account's share changes, the claim
does not change. It is marked *changed* instead, with the affected entries flagged, so the
administrator can approve the amount that was asked for or reject the claim for you to claim the
month again. Usage reported for the month after you submitted is not added to the claim.

Submitting, withdrawing, approving, rejecting, paying and exporting a claim are written to the
[audit log](audit-log.md#ai-reimbursement-claims).

## Budgets and alerts

A **monthly budget** caps what AI usage may cost in a calendar month (UTC) and sends an email when the
month's spend reaches a share of it. A budget covers one of:

- **a company account**, for example a corporate gateway or a company plan. Administrators set it on
  the account's page under **Admin → AI billing accounts**.
- **everything one payer pays**: *Employer total* is all the AI spend the company carries, every
  account and project together; *Employee total* is everything people pay themselves. Administrators
  set these under **Payer budgets** on the same page.
- **one of your own accounts**. You set it on the account's page under **My AI billing accounts**
  (while AI subscription self-service is on), and administrators can set it for you.

Each budget has an amount and a currency, and **alert thresholds**: the percents of the budget that
send an email, *80, 100* unless you change them (up to five, from 1 to 1000, so *150* warns when
spend is half again over). **Send alerts** off keeps the budget and its progress without emails.

Spend is what the [spend report](../projects/ai-spend-reports.md) counts for the month so far:
pay-per-token spend plus seat cost, the share of subscription fees carried by the usage (and, for a
company plan, the fee of seats nobody holds). It is converted into the budget's currency at each
amount's own day; spend without an exchange rate yet is not counted, and the budget says so.

Specivo checks the budgets every hour. When the month's spend reaches a threshold, the active
administrators get an email, and for a personal account its owner too, each in their own language.
**Each threshold sends one email a month**: later checks, and raising the amount, do not send it
again; the next month starts over. On the first day of a month the check also looks at the month
that just ended, so usage reported late still alerts.

Progress is shown as a bar with the spend and percent in the **Budget this month** column of the
account lists, on each account's page with the alerts already sent, under **Payer budgets**, and
under **Budgets this month** below the instance report in **Admin → AI spend**. Only administrators
see budgets on company accounts and payers; a personal account's budget is seen only by its owner and
administrators. Setting, changing and removing a budget on a company account or a payer, or somebody
else's, and every alert sent, are written to the [audit log](audit-log.md#ai-spend-budgets).

## With your AI agent

The `specivo_ai_billing_accounts` tool works like the pages, for the person whose API key the agent
uses:

| Operation | What it does |
|---|---|
| `list` | The company accounts and yours. `owner="instance"` lists the company accounts only; administrators pass a login for somebody else's. `include_inactive` adds switched-off ones. |
| `create` | Adds an account: `slug`, `name`, `kind`, `payer`, and optionally `reimburse_share` (a string such as `"0.5"`), `route_patterns`, `tool_slugs`, `default_cost_type`. `owner="instance"` adds a company account (administrators). |
| `update` | Changes one account (`account_id` plus the fields). `is_active=true` switches it on again; `""` clears the share or the default cost type. |
| `deactivate` | Switches one account off. |

A key limited to some projects is refused. Every change the tool makes is written to the
[audit log](audit-log.md#ai-billing-accounts) as `ai_billing_accounts_changed`. The
`specivo_ai_subscriptions` tool shows the billing account of each subscription.

The `specivo_ai_reimbursement_claims` tool handles claims: `list` (yours; administrators pass
`user="all"` or a login, and `status`), `get`, `create` (`month` as `YYYY-MM`, optional `currency`
and `note`), `refresh`, `submit`, `withdraw`, `delete` and `export` (the CSV); administrators
`approve`, `reject` (optional `reason`) and `paid`. It needs an unlimited key.

The `specivo_ai_spend_budgets` tool handles budgets: `list` (every budget for administrators, your
own for anyone else, each with this month's spend, percent and the alerts sent), `create`
(`billing_account` as an id or slug, or `payer` for administrators, with `amount` as a string such as
`"500"`, `currency` and optional `thresholds` such as `[50, 80, 100]`), `update` (`budget_id` plus
`amount`, `currency`, `thresholds` or `is_active`) and `delete` (`budget_id`). It needs an unlimited
key, and every change it makes is written to the audit log as `ai_spend_budgets_changed`.

## Through the API

Shares are decimal **strings** such as `"0.5"`, never JSON numbers.

```bash
# A company gateway (administrators)
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://specivo.example.com/api/v1/admin/ai-billing-accounts/ \
  -d '{"slug": "corp-gateway", "name": "Company gateway", "kind": "gateway", "payer": "employer",
       "route_patterns": ["corp-*"], "default_cost_type": "api"}'

# Your own API key, half reimbursed
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://specivo.example.com/api/v1/my/ai-billing-accounts/ \
  -d '{"slug": "my-key", "name": "My API key", "kind": "api_key", "payer": "employee",
       "reimburse_share": "0.5", "default_cost_type": "api"}'
```

| Endpoint | Who |
|---|---|
| `GET /api/v1/my/ai-billing-accounts/` (`?include_inactive=true`); `GET /api/v1/my/ai-billing-accounts/{id}/` | anyone signed in: company accounts and their own |
| `POST /api/v1/my/ai-billing-accounts/`; `PATCH /api/v1/my/ai-billing-accounts/{id}/`; `POST /api/v1/my/ai-billing-accounts/{id}/deactivate/` | the person, for their own; needs self-service |
| `GET /api/v1/admin/ai-billing-accounts/` (`?scope=instance`, `personal` or `all`, `?user_id=`); `POST` (with `owner_user_id` for a person's account); `GET`, `PATCH /api/v1/admin/ai-billing-accounts/{id}/`; `POST .../{id}/deactivate/` | administrators |

Each account in a response lists `subscription_ids`, the subscriptions it is the fee schedule of,
and each subscription has a `billing_account_id`. An API key limited to some projects cannot use
these endpoints.

Reimbursement claims have their own endpoints. Amounts are strings, the month is `YYYY-MM`:

```bash
# Claim March, then submit it
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://specivo.example.com/api/v1/my/ai-reimbursement-claims/ -d '{"month": "2026-03"}'
curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://specivo.example.com/api/v1/my/ai-reimbursement-claims/42/submit/
```

| Endpoint | Who |
|---|---|
| `GET`, `POST /api/v1/my/ai-reimbursement-claims/`; `GET`, `DELETE /api/v1/my/ai-reimbursement-claims/{id}/`; `POST .../{id}/refresh/`, `.../submit/`, `.../withdraw/`; `GET .../{id}/export/` (CSV) | the person, for their own claims |
| `GET /api/v1/admin/ai-reimbursement-claims/` (`?status=`, `?user_id=`); `GET .../{id}/`; `POST .../{id}/approve/`, `.../reject/` (`{"reason": "..."}`), `.../paid/`; `GET .../{id}/export/` | administrators, never on their own claim |

Budgets have their own endpoints. Amounts are strings; each budget in a response carries this
month's `spend`, `percent`, the thresholds `reached` and those `alerted`, and `unconverted` when some
spend has no exchange rate:

```bash
# The company's whole AI spend (administrators)
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://specivo.example.com/api/v1/admin/ai-spend-budgets/ \
  -d '{"payer": "employer", "amount": "5000", "currency": "USD", "thresholds": [80, 100]}'

# Your own API key
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://specivo.example.com/api/v1/my/ai-spend-budgets/ \
  -d '{"billing_account_id": 12, "amount": "200", "currency": "USD"}'
```

| Endpoint | Who |
|---|---|
| `GET`, `POST /api/v1/my/ai-spend-budgets/`; `GET`, `PATCH`, `DELETE /api/v1/my/ai-spend-budgets/{id}/` | the person, for budgets on their own accounts; changes need self-service |
| `GET /api/v1/admin/ai-spend-budgets/`; `POST` (`billing_account_id` or `payer`); `GET`, `PATCH`, `DELETE /api/v1/admin/ai-spend-budgets/{id}/` | administrators |

Each claim lists `changed_entries`, and each of its entries `changed` (`deleted`, `excluded`,
`billing` or `share`) when it is no longer what the claim froze.
