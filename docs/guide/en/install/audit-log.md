---
description: The security audit log — what Specivo records and what it does not, how long it keeps it, how admins read it, and the personal data it holds.
---

# Security audit log

Specivo keeps a security audit log of sign-ins, access changes, administration, single sign-on and
provisioning, and everything AI agents do over MCP. Every install records it; there is nothing to
turn on. Administrators read it under **Admin → Audit log** or through the API.

## What is recorded

Each row has a time, an **event type** (a code such as `login_failure` or `scim_user_created`), the
account that acted, and where the request came from: the client address, the request id and the
browser or client name (user agent). Rows about a project or an object name those as well. The
**details** hold ids, keys, counts and short names; see [What the details hold](#what-the-details-hold).

The viewer groups event types into categories:

| Category | What it holds |
|---|---|
| Sign-in | Password sign-ins and failures, refused credentials (`auth_failure`), password resets and changes, email address changes. |
| Access | Refused permission checks (`access_denied`), resource views through the API, searches, project membership and group changes, anonymous access settings. |
| Agents | What only agents do over MCP: reading and listing issues, wiki pages, projects, members and versions, and creating or changing issues, comments, wiki pages, versions, relations, recurring patterns and time entries. |
| Issues and wiki | Attachments, wiki page deletion and restore, sprint start and completion, tags, discarded timers. Rows written by an agent are marked **MCP**. |
| Administration | Project key renames, repository integrations moved to another provider, metadata schema changes, deactivated accounts, currencies switched on or off, reporting currency changes, exchange rates added, edited, deleted or imported, exchange rate settings and the rate fetch switched on or off, AI model prices added, changed or deleted, and bundled prices loaded. Admin actions on users (created, locked, password reset, API keys) are under Access as `member_change`. |
| Single sign-on | Single sign-on sign-ins and failures, accounts created, linked or unlinked, role sync, provider configuration. |
| Provisioning | SCIM user and group changes, refused SCIM requests, SCIM token changes. |
| Sessions | Revoked sessions, single sign-on logouts (including back-channel), sessions the identity provider refused to renew. |

Every MCP tool call is recorded with `source: mcp` in its details, including the client address,
request id and user agent of the call.

## What is not recorded

- **People's edits to issues and wiki pages.** Editing an issue, commenting or changing a wiki page
  in the web interface or through the REST API is not an audit event. Those changes are in the
  issue's history and the wiki page's versions, with who made them and when. The same edits made by
  an agent over MCP are in both places.
- **Successful permission checks.** Opening a page you are allowed to see writes nothing; a refused
  check does (`access_denied`).
- **Requests without credentials.** A signed-out browser tab, a crawler or a port scanner calling
  the API writes nothing, and neither does a browser session that simply expired.
- **Repeated refusals.** A credential that is presented and refused (a bad token, or an invalid,
  inactive or expired API key) is recorded as `auth_failure` once a minute per client address and
  reason, not once per request. API and MCP refusals are counted apart, and those from the MCP
  endpoint are marked **MCP**.
- **Anonymous reading** of public projects.

## Reading it

**Admin → Audit log** (`/admin/audit-log/`) lists events newest first. Filter by category, event
type, user login, project key, a date range and client address; the dates are days in your
timezone and the page starts with the last 30 days. Times are shown in your timezone. **Show** on a
row opens its details, the request id and the user agent. A user or project that has been deleted
since shows as `#` and its id.

The same data is at `GET /api/v1/admin/audit-logs/`, with the filters `event_type`, `event_types`
(repeat it for several), `user_id`, `login`, `project_id`, `ip_address`, `after`, `before`,
`offset` and `limit`. Each item carries `user_login` and `project_key`. An unknown event type or
login answers `422`.

Only instance administrators can read the log. An administrator's API key works for the API; a key
limited to some projects does not.

## Retention

Rows are kept for **12 months** by default, counted in whole calendar months: on 4 November 2026
the oldest kept day is 1 November 2025. A daily job (also run when a worker starts) drops older
months. Change it with an environment variable, then restart the stack:

| Variable | Meaning |
|---|---|
| `AUDIT_LOG_RETENTION_MONTHS` | Whole months of audit log to keep before the current one. Default `12`. `0` keeps everything. |

The setting is deliberately not in the admin pages: how long security evidence is kept should not
be changeable from an administrator's session or API key.

The log is stored in monthly partitions of the `security_audit_logs` table, so dropping a month is
quick. If the job cannot get a lock on the table within five seconds it gives up and tries again
the next day. The log is part of the database, so the [backup](backup-restore.md) includes it.

## What the details hold

Ids, keys, slugs, names and titles of objects (file names, page titles, sprint names), codes,
counts, logins of accounts, group names, and search text cut to 500 characters. Never tokens,
secrets or anything typed as a password, and never the text of issues, comments, descriptions or
wiki pages.

A failed sign-in records the login only when it names an existing account; otherwise the row says
`login_known: false`, so a password typed into the login field by mistake is not stored. A password reset request treats
the address the same way: it is stored only when it matches an account, otherwise the row says
`email_known: false`.

### Email address changes

| Event | Account | Details |
|---|---|---|
| `email_change_requested` | the person | `new_email`, `address_in_use` (another account uses it: no link was sent), `resend` |
| `email_change_failed` | the person | `reason`: a wrong password, a missing fresh sign-in at the identity provider, or a confirmation link that was used, replaced, cancelled, expired, or whose address another account took meanwhile |
| `email_changed` | the person, or the administrator | `by` (`user` or `admin`), `target_user_id`, `target_login`, `old_email`, `new_email`; a change by the person adds `revoked_sessions` |

A confirmation link that matches no request writes nothing. See [Your profile](../getting-started/your-profile.md#changing-your-email-address).

### Repository provider changes

| Event | Account | Details |
|---|---|---|
| `repository_provider_changed` | the person who saved the change | `old_provider`, `new_provider`, `old_repository`, `new_repository`, `secret_rotated`; the row names the project and the integration (`repository_integration` and its id) |

Changing an integration's provider gives it a new webhook URL and a new secret; the secret itself is
never logged. Other edits of a repository integration are not audit events. See
[Repository integrations](../integrations/repositories.md#change-the-provider).

### Currencies

| Event | Account | Details |
|---|---|---|
| `currency_toggled` | the administrator | `code`, `is_active` |
| `reporting_currency_changed` | the administrator, or the project manager | `scope` (`instance` or `project`), `project_key` for a project, `old`, `new`; a project's `null` means it follows the instance |

Saving the value that is already set writes nothing. See
[Currencies and the reporting currency](currencies.md).

### Development links

| Event | Account | Details |
|---|---|---|
| `development_link_detached` | the person who detached it | `issue`, `item_type` (`commit` or `pull_request`), `item_id`, `item` (the commit SHA, or `#N` / `!N`), `repository`; the row names the project and the issue |
| `development_link_moved` | the person who moved it | the same, plus `target_issue` and `target_project_id` |

A commit or pull request detached from an issue, or moved to another, through the API. Commit
messages and pull request titles are never logged. See
[Repository integrations](../integrations/repositories.md#correcting-links-detach-and-move).

### Exchange rates

| Event | Account | Details |
|---|---|---|
| `exchange_rate_created` | the administrator | `rate_id`, `rate_date`, `base`, `quote`, `rate`, `source` (`manual`) |
| `exchange_rate_updated` | the administrator | the same, plus `changes`: each changed field with `old` and `new` |
| `exchange_rate_deleted` | the administrator | the deleted rate: `rate_id`, `rate_date`, `base`, `quote`, `rate`, `source` |
| `exchange_rates_imported` | the administrator | `rows`, `created`, `updated`, `unchanged`, `date_from`, `date_to` |
| `exchange_rates_fetch_toggled` | the administrator | `enabled`, `url` |
| `exchange_rate_settings_changed` | the administrator | `changes`: `pivot`, `max_age_days` or `fetch_url`, each with `old` and `new` |

A CSV preview, a refused import and saving unchanged values write nothing. The daily fetch itself is
not an audit event; its last outcome is shown on the admin page. See [Exchange rates](exchange-rates.md).

### AI model prices

| Event | Account | Details |
|---|---|---|
| `ai_model_price_created` | the administrator | `price_id`, `provider`, `model`, `currency`, `effective_from`, `effective_to`, `source` (`admin`), `is_active`, `prices` (each set price per 1M tokens); `closed_price_id` when it ended the previous price of the model |
| `ai_model_price_updated` | the administrator | the same, plus `changes`: each changed field (`effective_to`, `is_active`, `aliases`, `note`) with `old` and `new` |
| `ai_model_price_deleted` | the administrator | the deleted price, as for `ai_model_price_created` |
| `ai_model_prices_seeded` | the administrator, or none when a worker loaded them at start | `as_of` (the bundled list's date), `inserted`, `closed`, `history_inserted`, `aliases_updated`, `unchanged`, `skipped` (counts) |

Saving unchanged values and loading a bundled list that changes nothing write nothing. The
installation's first load runs in the database migration and is not audited. See
[AI model prices](ai-model-prices.md).

## Personal data

The audit log holds personal data, readable by every instance administrator:

- **Client addresses (IP)** and **user agents** of the people and agents it records.
- **Logins** of the accounts that acted or were acted on.
- **Email addresses** in email change events: the old and the new address, and the address a
  change was requested for.
- **Search text** people and agents entered (up to 500 characters).

Keep the retention period no longer than you need, mention the log in your privacy notice, and
include it when you answer a request for someone's data. Behind a reverse proxy, set
[`TRUSTED_PROXIES`](configuration.md#client-addresses-behind-a-proxy) so the log records the
client's address rather than the proxy's.
