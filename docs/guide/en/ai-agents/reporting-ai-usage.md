---
description: Report the token usage of AI coding tools (Claude Code, Codex, Gemini CLI) to Specivo with the AI usage ingest API — batches, idempotent buckets, UTC days, issue attribution, and reporting for teammates.
---

# Reporting AI usage to Specivo

Specivo can track what your AI coding tools use: tokens, requests, the model, and the issue the
work was for. It prices that usage itself and shows it next to the time logged on issues and in
the project's spend reports. Usage gets in through one endpoint, the **AI usage ingest API**.
A reporter program reads the logs your tools write locally and sends them there.

This page is for the developer who sets up or writes such a reporter. The full contract, with
JSON Schemas and examples, is published in the Specivo repository at
[`docs/contracts/ai-usage/v1/`](https://github.com/specivo/specivo/tree/main/docs/contracts/ai-usage/v1).
Anyone can implement it.

!!! note "Agents cannot count their own tokens"
    An AI agent connected over MCP does not see its own token counters, so it cannot report
    accurate usage itself. Usage comes from the tool's local logs, read by a reporter that
    runs after a session or on a timer.

## Before you start

1. **An API key.** Create one under **Your profile → API keys** (see
   [API keys & scopes](api-keys.md)). The account that owns the key is the *reporter* of every
   record it sends.
2. **Permissions.** The account needs **Log AI usage** in the projects your usage is
   attributed to. Usage without a project needs no permission. To report for other people,
   it needs **Log AI usage for others** (see [Reporting for teammates](#reporting-for-teammates)).
3. **Check the server.** `GET /api/v1/ai-usage/ingest` returns the contract versions the
   server accepts, its limits, its clock and the AI tools it knows:

```bash
curl -s -H "Authorization: Bearer $SPECIVO_API_KEY" \
  https://your-specivo-host/api/v1/ai-usage/ingest
```

```json
{
  "versions": ["specivo.ai-usage.v1"],
  "max_records": 500,
  "max_bytes": 1048576,
  "max_age_days": 400,
  "rate_limit": {"requests": 60, "window_seconds": 60},
  "server_time": "2026-11-06T12:00:00Z",
  "tools": ["claude-code", "codex", "gemini-cli"],
  "currencies": ["EUR", "THB", "USD"]
}
```

## Send a batch

`POST /api/v1/ai-usage/ingest` takes a batch of up to 500 records:

```bash
curl -s -X POST https://your-specivo-host/api/v1/ai-usage/ingest \
  -H "Authorization: Bearer $SPECIVO_API_KEY" \
  -H "Content-Type: application/json" \
  -d @batch.json
```

```json
{
  "schema": "specivo.ai-usage.v1",
  "client": {"name": "my-usage-reporter", "version": "0.1.0"},
  "records": [
    {
      "idempotency_key": "752cc282663658a7ec3ea666eb7b7aa62c4aeda5191f2594c426edb9c558bcc4",
      "tool": "claude-code",
      "provider": "anthropic",
      "model": "claude-opus-4-6",
      "session_id": "0f6c2a1e-session",
      "usage_date": "2026-11-05",
      "started_at": "2026-11-05T09:10:00+07:00",
      "ended_at": "2026-11-05T10:40:00+07:00",
      "request_count": 42,
      "tokens": {"input": 1200, "output": 9000, "cache_write_5m": 30000, "cache_read": 900000},
      "attribution": {"issue_ref": "ACME-12", "git_branch": "feat/acme-12-login"}
    }
  ]
}
```

Only `idempotency_key`, `tool`, `model`, `usage_date` and `tokens` are required. The answer
is HTTP 200 with one result per record:

```json
{
  "schema": "specivo.ai-usage.v1",
  "server_time": "2026-11-06T12:00:01Z",
  "summary": {"received": 1, "created": 1, "updated": 0, "unchanged": 0, "rejected": 0},
  "results": [
    {
      "index": 0,
      "idempotency_key": "752cc282663658a7ec3ea666eb7b7aa62c4aeda5191f2594c426edb9c558bcc4",
      "status": "created",
      "entry_id": 1042,
      "revision": 1,
      "attribution": {"method": "explicit", "project_key": "ACME", "issue_ref": "ACME-12"},
      "warnings": [],
      "error": null
    }
  ],
  "warnings": []
}
```

Each record is stored or rejected on its own. A rejected record has `status: "rejected"` and
an `error` with a `code`, a `message` and the `field` at fault; the others are still stored.

## One record, one bucket

A record holds the **full totals** of one *bucket*: one session of one tool, with one model,
on one **UTC** day. Specivo keeps one entry per bucket, keyed by its `idempotency_key` (per
user).

- Send the whole bucket every time, not what changed since the last send. Specivo
  **replaces** the totals; it never adds them up.
- Sending the same totals again changes nothing (`unchanged`), so resending is safe. After a
  crash or a lost connection, just send the day again.
- A good key is the SHA-256 of `v1|<tool>|<session_id>|<model>|<usage_date>`.
- If someone excluded the entry from reports in Specivo, later sends update it but leave it
  excluded.

## Days and times are UTC

- `usage_date` is the day **in UTC**. A session that runs past midnight UTC becomes two
  records, one per day. In Bangkok (UTC+7), for example, the UTC day changes at 07:00 local
  time.
- `started_at` and `ended_at` need a time zone offset (`Z` or `+07:00`). `started_at` must fall
  on `usage_date` in UTC, and `ended_at` no later than the end of that day.
- `usage_date` cannot be in the future or more than 400 days ago.
- Specivo prices each record at `started_at` (or at the start of its UTC day), with the
  prices valid at that moment.

Specivo still shows days in each person's own time zone; around midnight the two can differ by
a few hours.

## Which issue the usage counts for

Specivo tries these hints in order and records which one it used:

1. **`attribution.issue_ref`**: the issue you named, for example from the `SPECIVO_ISSUE`
   environment variable (add `"issue_ref_source": "env"`).
2. **The agent's MCP session**: if an agent connected to Specivo's MCP server was working on an
   issue between `started_at` and `ended_at`, that issue.
3. **`attribution.commits`**: the issue your commits from the session are linked to.
4. **`attribution.git_branch`**: an issue key in the branch name, such as
   `feat/acme-12-login`.
5. **`attribution.project_key`**, or **`attribution.repo`** when it matches a repository
   connected to exactly one project.
6. Otherwise the entry is **unassigned**, and can be assigned in Specivo later.

What you name (`issue_ref`, `project_key`) must work: an unknown issue, or a project where the
usage cannot be logged, rejects the record. What Specivo works out by itself (steps 2 to 5) is
skipped with the warning `attribution_dropped` when it cannot be used, and the next hint is
tried.

Once an entry is attributed, later sends keep its attribution. A send that names a different
issue or project is rejected with `attribution_conflict`; move the entry in Specivo instead.

## Reporting for teammates

One shared account can report for a whole team with `"on_behalf_of": "<login>"` on each
record. For that, the account needs **Log AI usage for others** in the project, and the person
must be an active member of it. Usage without a project can be reported for someone else only
by an administrator.

Every entry shows which account and key logged it, every person sees all the usage logged in
their name, and logging for someone else is written to the
[audit log](../install/audit-log.md).

## Costs

Send tokens; Specivo computes the cost from its [AI model prices](../install/ai-model-prices.md).

- `cost_type` says how the usage is paid: `api`, `subscription`, `local`, `free` or `unknown`.
  Leave it out and Specivo uses `subscription` when the person has an
  [AI subscription](../install/ai-subscriptions.md) covering the tool, otherwise `unknown`.
- When the tool knows what the provider charged, send it as `reported_cost`
  (`{"amount": "1.2345", "currency": "USD"}`). For `api` usage it is the spend.
- `input` tokens must not include cache reads or writes; send those in `cache_read`,
  `cache_write_5m` and `cache_write_1h`.

## Limits and errors

| Answer | Meaning |
|---|---|
| 200 | The batch was read. Check each result's `status`. |
| 400 | The body is not JSON (`invalid_json`), the envelope is wrong (`invalid_batch`), or the server does not accept the `schema` version (`unsupported_schema`). |
| 401 | No API key, or a bad one. |
| 413 | Over 500 records or over 1 MiB (`batch_too_large`): split the batch. |
| 429 | Too many batches; wait for the `Retry-After` seconds. The limit is 60 batches per minute per account by default. |

Administrators change the rate limit with the environment variable
`AI_USAGE_INGEST_RATE_LIMIT_PER_MINUTE` (see [Configuration](../install/configuration.md)).

The contract's README lists every error and warning code, and the rules for new versions: new
optional fields can appear in v1 at any time, so ignore fields you do not know in answers.
