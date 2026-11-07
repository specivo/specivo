---
description: AI spend reports in Specivo — see what AI coding tools cost per person, model, tool, project, issue, branch and period, in one currency, with flat-rate subscription fees spread over the usage and API-equivalent value side by side.
---

# AI spend reports

The [AI usage](ai-usage.md) list shows single entries. The **spend report** answers the questions a
team lead or an administrator asks about them: who used which models and tools, on which projects
and issues, and what it cost, in one currency, with subscriptions included.

There are two reports:

- the **project spend report**: open a project's **AI usage** and click **Spend report**;
- the **AI spend** page for the whole instance: **Admin → AI spend**, for administrators.

![The spend report of a project, per person and week](../../assets/img/ai-spend-project.webp)

## Drill down

Both reports open as a drill-down: tiles with the **spend**, **entries**, **tokens** and **people**
of what you are looking at, a **Group by** switch, a table and a **Spend by …** card next to it.

- **Group by**: **Project** (on the AI spend page), **Person**, **Feature** (the issue), **Tool**,
  **Model** or **Sessions**. The AI spend page starts with projects, a project report with people.
- Each row has a colour, a name and a short line under it (how many people used a tool or worked on
  an issue, which tools a person used), its entries, tokens, spend and its **share** of the total.
- **Click a row to open it.** The row becomes a filter and the table moves to the next useful
  grouping: a project opens its people, a person their issues, an issue its sessions. A tool or a
  model opens the people who used it.
- The **path** above the title shows what you opened, for example *All projects › WEB › a person ›
  WEB-12*. Click any step to go back to it. Groupings already in the path are greyed out
  in the switch.
- **Sessions** lists the entries behind the current level, newest first, a page at a time: date,
  person, tool, model, tokens, cost type, spend, issue and branch. Spend includes the entry's share
  of its subscription where you may see seat cost. **Open in the entry list** shows the same entries
  on the AI usage list.
- **All amount columns** adds pay-per-use, seat cost, API-equivalent value and leverage to the table,
  with an **Entries** button per row (see [From a number to the entries](#from-a-number-to-the-entries)).
- A grouping with more than 30 rows shows the 29 largest by spend and one **Other** row with the
  rest; the CSV lists every row.

Everything you open is in the page address, so you can bookmark a level, reload it or send the
link to someone. They see it with their own rights: the same filters, only the entries they may see.

## Spend by person

Under the drill-down, **Spend by person** lists everyone in the current selection: their spend, the
tools they used and how their usage is paid. The colour of a person is the same as in the charts on
the page. **Show** opens that person in the drill-down.

How usage is paid follows the rules for seat cost below. The AI spend page names each person's plans
(*Claude Max 20x*, *Pay-per-use API*). A project report that shows seat cost says **Subscription
seat**, never the plan; anyone else sees only the cost types of the entries (*Subscription*,
*Pay-per-use API*, *Local model*, *Free*).

## What the numbers mean

| Measure | What it is |
|---|---|
| **Pay-per-use** | What the usage cost on top of flat-rate plans: API usage at its price, or at the cost the provider reported. Usage covered by a subscription adds nothing here. |
| **Seat cost** | The share of people's [AI subscriptions](../install/ai-subscriptions.md) carried by this usage (see below). |
| **Spend** | Pay-per-use plus seat cost: what the usage actually cost. |
| **API-equivalent value** | What the same tokens would have cost at pay-per-use [API prices](../install/ai-model-prices.md). It is an estimate, not a bill. |
| **Leverage** | The API-equivalent value of subscription usage divided by its seat cost. Above 1, the subscription costs less than paying per use would have; below 1, it costs more. Shown only where there is a seat cost. |
| **Tokens**, **Entries** | Token totals (hover a row for input, output, cache and reasoning), long-context tokens included, and how many entries count. Entries that have no price are counted in brackets. |

Spend and API-equivalent value are always shown side by side, so you can see both what you pay and
what the usage would be worth on pay-as-you-go.

Excluded entries never count. You see only the entries you may see on the AI usage list: everyone's
with the *All AI usage* visibility, otherwise your own.

## How subscriptions are shown

A subscription is a flat fee, not a price per token, so the report **spreads** it over the usage it
paid for:

1. Each month's fee is prorated to the days the subscription was active that month. A yearly plan
   counts a twelfth of its amount per month.
2. The fee is shared among the holder's entries of that month that the subscription covered,
   **weighted by their API-equivalent value**. An entry worth twice as much at API prices carries
   twice the share.
3. The shares use all of the holder's usage in the month, in every project. A share does not change
   with who opens the report or which dates it covers.

For example, a 100 USD plan whose holder used it for 30 USD of API-equivalent value in one project and
10 USD in another shows a seat cost of 75 USD in the first project and 25 USD in the second.

A month in which a subscription was not used, or only for usage that has no price, cannot be shared.
Its fee is **unallocated seat cost**. The AI spend page shows it on its own row, with a
**Subscriptions** link to the person's plans, and in the totals. Project reports never show it.

Project reports never show plan names; the AI spend page names them only in its leverage chart, for
administrators. **Seat cost is shown only to administrators, and to people who can
see every AI usage entry of the project in the period.** Anyone else sees pay-per-use spend and
API-equivalent value, with a note that seat cost is left out. This keeps people's subscriptions
private: a project report shows only the shares carried by that project's usage, never a whole fee.

## Choose what to see

![The AI spend page for the whole instance, per person and project](../../assets/img/ai-spend-admin.webp)

The period buttons are always shown; the other settings are under **Dates, filters and options**.

- **Period**: click **Last 7 days**, **Last 30 days**, **This month** or **Last month**, or pick
  the first and last day yourself. Days are UTC days.
- **Group by** and **Then by**: person, project (on the AI spend page), issue, tool, provider, model,
  cost type or branch. Choose **Nothing (totals)** for one line.
- **Per**: split each group per **day**, **week** (from Monday) or **month**. Opening such a row
  opens its group for that period's dates.
- **User**, **Tool**, **Model**, **Cost type**: narrow the usage. Unallocated seat cost has no tool
  or model, so it is left out when you filter by one, and likewise when you open a project, an issue
  or a branch.
- **Currency**: the [reporting currency](../install/currencies.md#reporting-currency) of the project
  (or of the instance on the AI spend page), or any active currency.
- **Exchange rates**: **Rate of each day** converts each day's amounts with that day's rate (within
  the [maximum age](../install/exchange-rates.md)); **Latest rate** uses the newest rate for
  everything.
- **Seat cost**: **Leave out** to see pay-per-use spend only.
- **Order**: rows are ordered by the group. **By spend** puts the largest spend first.

## Charts

Below the drill-down, **Charts** show the usage the report counts, with the same filters,
currency and exchange rates, so they add up to the totals:

- **API-equivalent value and spend over time**, split **by user**, **by tool** or **by model** (and
  **by project** on the AI spend page). Spend is a line; where you may see seat cost, it includes the
  seat cost, and a dashed line shows the seat cost alone.
- **Tokens over time**: input and output in one chart, cache reads and writes in another.
- The value **by user**, **by project** (AI spend page), **by model** and **by tool**: the eight
  largest, the rest as **Other**. These show pay-per-use spend, since seat cost is not split by
  model or tool; the table holds it.
- **Subscription leverage**, only where you may see seat cost. The AI spend page lists every plan
  with its holder, every person and the instance total: the fees counted in the period against the
  API-equivalent value of the usage they covered. A project report lists people only, each with the
  share of their fees carried by the project's usage, never plan names or whole fees.

The charts run from the first to the last day with usage in the chosen period (on the AI spend page,
also the days of plans whose fee nobody's usage carried), one point per day, week or month
depending on how long that is. **Day**, **Week** and **Month** pick a size yourself; a size that
would draw more than 120 points falls back to **Auto**. Entries without a price count as nothing, so
a period that has some is marked with an asterisk, and its tooltip says how many.

## Currencies

Every amount is converted into the report's currency. The line under the totals lists the exchange
rates used and the days they come from. Seat fees in another currency are converted at the rate of
the **last day of the month** (today for the current month).

An amount with no usable rate is never dropped and never counted as zero: it is shown after the
total as **plus, not converted**, in its own currency, with the reason (*no exchange rate* or *rate
too old*). Add the missing rates under **Admin → Exchange rates** and open the report again.

## From a number to the entries

Opening rows down to **Sessions** shows the entries in the report itself. With **All amount
columns**, every row also has an **Entries** button. It opens the AI usage list filtered to that row:
the same period, person, tool, model, issue, branch and so on. On the AI spend page it opens a list of the
instance's entries, unassigned ones included. Rows on an issue you cannot see have no button.

## Download

**Download CSV** saves the report as it is shown, with the filters of the path you opened: one line
per row (all of them, also those under **Other**) and a total line, the amounts in the report's
currency, and a column listing amounts that were not converted. At the **Sessions** level it lists
the spend per model.

The same report is available over the REST API (`GET /api/v1/projects/{key}/ai-usage/report/` and,
for administrators, `GET /api/v1/ai-usage/report/`, both with `format=csv`; `issue`, `branch` and,
on the instance report, `project` narrow it like the path, `-` meaning none) and to AI agents through
the `specivo_ai_usage_report` MCP tool, with the same rules about what each person may see.
