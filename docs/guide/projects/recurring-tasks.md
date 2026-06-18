---
description: Recurring tasks in Specivo — patterns that automatically generate issues on a schedule, with flexible or fixed anchor modes, date macros, and per-occurrence exceptions.
---

# Recurring tasks

A **recurring task pattern** is a template that generates issues on a schedule. Instead of
remembering to create the same issue every week or month, you define the pattern once and Specivo
creates each instance for you automatically.

Recurring tasks live at **Projects → (your project) → Recurring tasks** in the left sidebar.

<!-- screenshot: recurring pattern list page -->

## What a pattern is

A pattern is not an issue itself. It holds two things:

1. **A schedule** — when and how often to fire (daily, weekly, monthly, yearly, with all the
   refinements you need).
2. **An issue template** — the fields every generated issue is born with: subject, tracker, status,
   priority, assignee, description, and metadata.

Each time the schedule fires, Specivo creates a real issue in the project using the template. The
issue is fully ordinary — you can comment on it, move it through statuses, log time, and link it to
other issues — just like one you created by hand.

## Creating a pattern

Go to the project's **Recurring tasks** area and click **New pattern**.

<!-- screenshot: recurring pattern form -->

### Name and enabled toggle

Give the pattern a clear name you'll recognize in the list (for example, "Weekly status report" or
"Monthly database backup check"). The **Enabled** toggle controls whether the pattern actively
generates issues. You can disable a pattern temporarily without deleting it.

### Schedule

The schedule section is where you define the recurrence rule.

| Field | What it controls |
|---|---|
| **Frequency** | How often: **daily**, **weekly**, **monthly**, or **yearly** |
| **Repeat every** | The interval — `2` with frequency Weekly means every two weeks |
| **On weekdays** | Which days of the week (for weekly rules, or combined with set position for monthly) |
| **On days of month** | Specific day numbers (`1`, `15`, `-1` for the last day) — monthly only |
| **Set position** | The nth occurrence within the period (`1` = first, `2` = second, `-1` = last) — combine with a weekday to express "the second Tuesday" |
| **Start** | The date and time the series is anchored to (the first possible occurrence) |
| **Timezone** | The IANA timezone for occurrence computation, e.g. `Europe/London`. Occurrences fire at the same wall-clock time year-round, adjusting for daylight saving. |

!!! tip "Live preview"
    The form shows a **Next occurrences** preview as you build the rule, so you can verify the
    schedule looks right before saving.

#### Examples

- **Every Monday**: frequency Weekly, no weekday selected (anchors to the start date's weekday — make the start date a Monday), or select MO.
- **Every two weeks on Tuesday and Thursday**: frequency Weekly, interval 2, weekdays TU and TH.
- **The last day of the month**: frequency Monthly, on days of month `-1`.
- **The second Tuesday of every month**: frequency Monthly, weekday TU, set position `2`.

### Start date and timezone

The **Start** field is the series anchor: it sets both the time of day and the earliest possible
occurrence. Set the **Timezone** to the IANA zone that makes sense for the people doing the work.
Specivo keeps the wall-clock time constant across DST transitions — a 09:00 Monday pattern stays
at 09:00 local time in winter and summer.

### Anchor mode

**Anchor mode** is the most important behavioural choice:

**Fixed** (default)
: Every occurrence fires on its scheduled calendar date regardless of whether the previous instance
is closed. If the generator missed a run (downtime, maintenance), any overdue occurrences are created
the next time it runs and they stack up. Use fixed for calendar-driven events that should happen on
a specific date no matter what — standups, weekly reports, sprint ceremonies.

**Flexible**
: The next occurrence is only created once the previous instance is closed (status in the done or
closed category). There is at most one open instance at a time. Use flexible for chores where "do
it again" only makes sense after "you finished the last one" — database maintenance, security
audits, one-at-a-time review cycles.

When anchor mode is **Flexible**, an additional **Base date** setting appears and controls where
the next occurrence is measured from:

- **Scheduled date** — the next occurrence is measured from when the previous one *was supposed* to
  happen. Closing an instance late doesn't push the series forward.
- **Completion date** — the next occurrence is measured from when the previous instance actually
  closed. Useful when the gap between instances matters more than the calendar date.

### Creation lead time

**Creation lead time (days)** controls how far ahead issues are generated. The default is 30 days:
a background job runs roughly every hour and creates any issues due within the next 30 days that
haven't been generated yet. Increase this if you want issues to appear further in advance; decrease
it if you only want them to appear close to the due date.

## The issue template

The lower half of the form defines what each generated issue looks like.

| Field | What it sets |
|---|---|
| **Subject** | The issue title — supports [date macros](#date-macros) |
| **Tracker** | The issue type (Bug, Feature, Task, etc.) |
| **Status** | The starting status; leave as "Tracker default" to use the tracker's configured default |
| **Priority** | The starting priority |
| **Default assignee** | Who the issue is assigned to when created |
| **Description template** | The issue body — also supports date macros |

### Carry over and reset

The **Carry over & reset** section controls which template values are applied to each generated
instance:

- **Carry over description** — apply the description template to generated issues (on by default).
- **Carry over assignee** — assign generated issues to the default assignee (on by default).
- **Carry over metadata** — apply template metadata to generated issues (on by default).
- **Carry over estimated hours** — set estimated hours from the template (on by default).
- **Reset checklist** — if the template metadata includes boolean (checklist-style) fields, reset
  them to unchecked on each new instance. Useful for recurring checklists where each instance
  should start fresh.

### Assignee rotation

Instead of assigning every instance to the same person, you can **rotate** across a list of
members. Select the members to rotate through; Specivo assigns each new occurrence to the next
person in the list, cycling back to the beginning. Rotation overrides the default assignee when
both are set.

## Date macros

The template **Subject** and **Description** support date macros that expand to each occurrence's
local date (in the pattern's timezone). This keeps generated issues distinct — rather than ten
identical "Status report" issues, you get "Status report — 18 June 2026", "Status report — 25 June
2026", and so on.

| Macro | Expands to | Example (18 June 2026) |
|---|---|---|
| `{{year}}` | Four-digit year | `2026` |
| `{{quarter}}` | Quarter with Q prefix | `Q2` |
| `{{month}}` | Full month name | `June` |
| `{{month_num}}` | Two-digit month number | `06` |
| `{{day}}` | Two-digit day of month | `18` |
| `{{weekday}}` | Full weekday name | `Thursday` |

Month and weekday names are localized to the workspace language. If your workspace is set to French,
`{{month}}` outputs `juin`; if set to Thai, it outputs `มิถุนายน`.

!!! note "`{{quarter}}` already includes the Q"
    Write `{{quarter}} {{year}} review`, not `Q{{quarter}} {{year}} review`. The macro already
    outputs `Q2`, so the second form would produce `QQ2 2026`.

Example: a subject of `{{weekday}} standup — {{day}} {{month}} {{year}}` generates
`Thursday standup — 18 June 2026` for the occurrence on 18 June 2026.

Unknown macro names are left unchanged in the output, so a typo like `{{mnth}}` appears literally
rather than silently disappearing.

## How generation works

A background job runs roughly every hour and generates any issues that are due within the look-ahead
window and haven't been created yet. The job is:

- **Idempotent** — running it twice creates no duplicates.
- **Catch-up aware** — if a generation was missed (for example during scheduled maintenance), the
  next run creates all overdue occurrences at once (in fixed mode).
- **Fault-isolated** — a problem with one pattern never stops generation for others.

Each generated issue shows a **"Created from recurring pattern"** line in its **Activity** tab,
with a link back to the pattern. This is the provenance trail — you can always find which pattern
produced a given issue.

## Viewing a pattern

Click a pattern name from the **Recurring tasks** list to open its detail page. The detail page
shows:

- **Schedule** — the configured rule, timezone, anchor mode, and lead time.
- **Template** — the subject, carry-over settings, and rotation roster.
- **Upcoming occurrences** — the next scheduled dates within the look-ahead window. Managers can
  click **Skip** next to any upcoming occurrence to exclude it.
- **Live preview** — a refreshable view of the next occurrences the engine would generate.
- **Generated instances** — a table of every issue the pattern has produced, with its status and
  scheduled occurrence date.

<!-- screenshot: recurring pattern detail page -->

## Editing a pattern

From the detail page, click **Edit**. A notice on the form reminds you: **editing a pattern affects
future instances only**. Issues already generated keep the values they were created with; only
occurrences not yet materialized pick up the new template or schedule.

If you need the change to apply from a specific future date while preserving the history before it,
ask a project manager or use an AI agent to perform a **this-and-future split** via the API. This
terminates the old series just before the chosen occurrence and starts a fresh pattern from that
point forward.

## Skipping a single occurrence

From the pattern detail page, the **Upcoming occurrences** list shows each scheduled date with a
**Skip** button. Skipping removes that one date from the series — the issue is never created. If an
untouched issue was already generated for that occurrence (still in an open status with no progress
recorded), it is removed. Skipped occurrences do not count as completions in flexible mode.

## Disabling and deleting a pattern

- **Disable** — uncheck **Enabled** and save. No new issues are generated, but existing ones
  remain and the pattern's history is preserved. Re-enable it at any time.
- **Delete** — deletes the pattern. Issues already generated survive (their link back to the pattern
  is cleared); skip and override exceptions are removed.

## Through an AI agent

Recurring patterns can also be created, updated, and inspected by a connected AI agent. See
[AI agents & MCP](../ai-agents/index.md) and the [tool catalog](../ai-agents/capabilities.md) for
the available tools.
