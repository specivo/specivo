"""Dynamic setup guide generation for the Specivo MCP server.

Builds a concise agent configuration guide from the actual registered
MCP tools, keeping output under ~3 KB.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

# Tool-table rows are maintained here so the guide stays accurate even
# if a tool docstring is reworded.  Order matches the logical grouping.
_TOOL_SUMMARIES: dict[str, str] = {
    "specivo_whoami": "Return authenticated user identity (user_id, login, etc.)",
    "specivo_list_projects": "List all visible projects",
    "specivo_list_issues": (
        "List issues (with tags); filter by status/sprint/metadata/parent/assignee/tag/release, sort, paginate"
    ),
    "specivo_show_issue": (
        "Issue details: parent, children, tags, commits, PRs, links to/from, source number; search= for a section"
    ),
    "specivo_issue_development": (
        "Development activity of an issue as JSON: commits, PRs, reviews, CI, deployments, releases; "
        "summary and a paged timeline (kinds=, limit=, offset=)"
    ),
    "specivo_create_issue": "Create an issue, optionally with parent_ref and tags (call list_lookups first for IDs)",
    "specivo_update_issue": "Update issue fields or re-parent (parent_ref); lock_version handled automatically",
    "specivo_move_issue": "Move an issue to another project (new KEY-N; the old ref still resolves)",
    "specivo_edit_description": "Search-and-replace in issue description (token-efficient)",
    "specivo_add_comment": "Add a journal comment to an issue",
    "specivo_list_comments": "List comments on an issue, paginated; include_changes=true adds named field changes",
    "specivo_search": (
        "Search issues/wiki. mode='hybrid' (default), 'keyword', or 'semantic'; expand=1 adds each top hit's links"
    ),
    "specivo_list_wiki_pages": "List wiki pages for a project",
    "specivo_read_wiki": "Read a wiki page by slug (with its links and backlinks)",
    "specivo_get_links": (
        "Link graph of an issue, wiki page, tag, version, sprint, component or user: backlinks, links, "
        "relations, members; depth 1-2 (3 from a hub); to= finds a path; filter=; format='compact'; "
        "include_suggestions=true adds suggested links"
    ),
    "specivo_link_suggestion": (
        "Accept (as a relation or a typed link) or dismiss a suggested link between two issues or wiki pages"
    ),
    "specivo_create_wiki": "Create a new wiki page, optionally with tags (slug auto-derived from title)",
    "specivo_update_wiki_metadata": "Update wiki page parent, title, or protected flag",
    "specivo_delete_wiki": "Soft-delete a wiki page (moves to trash)",
    "specivo_restore_wiki": "Restore a wiki page from trash",
    "specivo_edit_wiki": "Search-and-replace in wiki page content",
    "specivo_append_wiki": "Append text to a wiki page (end or after a heading)",
    "specivo_read_wiki_section": "Read a single section from a wiki page by heading",
    "specivo_replace_wiki_section": "Replace a section body in a wiki page (heading preserved)",
    "specivo_list_lookups": "Get tracker/status/priority/activity IDs for this instance",
    "specivo_list_members": "List project members with roles and user IDs",
    "specivo_list_metadata_schemas": "Discover custom metadata fields for a project (returns schema id)",
    "specivo_create_metadata_schema": "Create a metadata schema (manage_project; audited)",
    "specivo_update_metadata_schema": "Patch a metadata schema (manage_project; audited)",
    "specivo_delete_metadata_schema": "Delete a metadata schema, safe-delete (manage_project; audited)",
    "specivo_metadata": "Get, set or delete one metadata key on an issue or wiki page (values checked against schemas)",
    "specivo_list_tags": "List a project's tags with usage counts -- call before tagging and reuse names",
    "specivo_tag": "Get, add, remove or set the tags on an issue (ACME-12) or wiki page (wiki:ACME/<slug>)",
    "specivo_create_tag": "Create a project tag with an optional color (manage_project)",
    "specivo_update_tag": "Rename or recolor a tag (manage_project)",
    "specivo_delete_tag": "Delete a tag and detach it everywhere (manage_project)",
    "specivo_list_relations": "List relations for an issue",
    "specivo_add_relation": "Create a relation between two issues",
    "specivo_remove_relation": "Remove a relation by ID",
    "specivo_log_time": "Log time against a project or issue",
    "specivo_timer": "Your own timer: op='get', 'start' (issue_ref), 'stop' (activity, comments) or 'discard'",
    "specivo_agent_session": (
        "This connection's agent session: op='set' (your client's session id, which selects its own session on "
        "a shared connection, parent id, issue_ref; '' clears it) or 'get'"
    ),
    "specivo_log_ai_usage": (
        "Log AI tokens an AI tool used (from a hook, the reporter or /cost output -- never estimated)"
    ),
    "specivo_ai_usage": "AI usage entries: op='list', 'get', 'update', 'exclude' or 'include'",
    "specivo_ai_usage_report": (
        "AI spend report: usage grouped by release, sprint, user, model, tool, project, issue, cost type, branch, "
        "attended or unattended run, agent profile, payer, billing account, route "
        "and period, in one currency"
    ),
    "specivo_ai_subscriptions": (
        "Your AI subscriptions (seats): op='list', 'presets', 'add' (several periods at once), 'update', 'end' "
        "or 'delete'; warns about overlaps and gaps. Administrators: company seat plans (company=true) and their "
        "members ('members', 'add_member', 'end_member', 'remove_member')"
    ),
    "specivo_ai_billing_accounts": (
        "AI billing accounts (gateways, company plans, personal keys) and who pays: op='list', 'create', "
        "'update' or 'deactivate'"
    ),
    "specivo_ai_reimbursement_claims": (
        "Monthly AI reimbursement claims: op='list', 'get', 'create', 'refresh', 'submit', 'withdraw', "
        "'delete', 'export'; administrators 'approve', 'reject', 'paid'"
    ),
    "specivo_ai_spend_budgets": (
        "Monthly AI spend budgets on a billing account or payer, alerted at thresholds: op='list', 'create', "
        "'update' or 'delete'"
    ),
    "specivo_list_versions": "List versions/milestones",
    "specivo_list_version_issues": "List the issues of a version, or unversioned issues (version_id=0)",
    "specivo_create_version": "Create a version/milestone",
    "specivo_update_version": "Update a version/milestone",
    "specivo_delete_version": "Delete a version (fails if issues reference it)",
    "specivo_list_sprints": "List sprints/iterations for a project",
    "specivo_list_sprint_issues": "List the issues of a sprint, or backlog issues (sprint_id=0)",
    "specivo_create_sprint": "Create a new sprint (starts as planned)",
    "specivo_update_sprint": "Update sprint name, goal, or dates",
    "specivo_start_sprint": "Start a planned sprint (one active per project); noted in each issue's history",
    "specivo_complete_sprint": (
        "Complete an active sprint with velocity snapshot; unfinished issues move to backlog or another "
        "open sprint of the project, and each issue's history notes it"
    ),
    "specivo_list_recurring_patterns": "List recurring task patterns with rule and next occurrence",
    "specivo_create_recurring_pattern": "Create a recurring task pattern that generates issues on a schedule",
    "specivo_update_recurring_pattern": "Update a recurring task pattern",
    "specivo_delete_recurring_pattern": "Delete a recurring task pattern",
    "specivo_skip_recurrence_occurrence": "Skip one scheduled occurrence of a recurring pattern",
    "specivo_list_recurrence_occurrences": "Preview upcoming occurrences of a recurring pattern",
    "specivo_setup_guide": "Return this setup guide",
}


def generate_setup_guide(fmt: str = "generic", mcp_server: MCPServer | None = None) -> str:
    """Build the agent setup guide.

    Args:
        fmt: "claude" for CLAUDE.md, "cursor" for .cursorrules, "generic" otherwise.
        mcp_server: If provided, tool table is generated from actual registered tools.
    """
    tool_rows = _build_tool_table(mcp_server)
    header = _format_header(fmt)
    footer = _format_footer(fmt)

    return f"""{header}
## Key concepts

- **project_key** -- Uppercase identifier, e.g. `ACME`. Never use project name or numeric ID.
- **issue_ref** -- Display key like `ACME-12`. Never pass raw numeric IDs.
- **slug** -- URL-friendly wiki page identifier. Use `list_wiki_pages` to discover.
- **IDs are instance-specific** -- tracker/status/priority/activity IDs vary per install. Call `list_lookups`.

## Tools

| Tool | Description |
|------|-------------|
{tool_rows}

## Standard workflows

### Find context before coding
1. `specivo_search(query, project_key)` -- relevant issues/wiki (default `mode="hybrid"`)
2. `specivo_show_issue(issue_ref)` -- read issue details (includes `Parent:`, `Children:`, `Tags:` and
   `Comments: N`, and `Commits: N` with the newest linked commits and `Pull requests: N` with their state
   when a connected repository referenced the issue; an imported issue also has `Imported from: RT #123`,
   and `specivo_search` finds it by that number: `RT #123`, `#123`, or a key such as `PROJ-7`)
   For builds, deployments and releases call `specivo_issue_development(issue_ref)` (filter with
   `kinds=["ci", "deployment"]`); `specivo_show_issue` only sums them up on its `Development:` line.
3. `specivo_list_comments(issue_ref)` -- page through comment history when `Comments: N` > 0
4. `specivo_read_wiki(project_key, slug)` -- read knowledge base
5. Follow the links: `specivo_show_issue` and `specivo_read_wiki` list `Links to:`, `Linked from:`
   (backlinks from issues, comments and wiki pages) and `Related:`. For more, filters or a second hop,
   `specivo_get_links(ref, direction="in"|"out"|"both", depth=1|2, types=[...])` -- `ref` is `ACME-12`
   or `wiki:ACME/<slug>`; it returns JSON nodes and edges, only what you may see. Attachments are counted
   on each node (`attachments`), not listed, unless `types=["attachment"]`.
6. Everything in one release, component, tag, sprint or person's queue: start `specivo_get_links` from
   `version:ACME/<name>`, `component:ACME/<value>`, `tag:ACME/<name>`, `sprint:ACME/<name>` or
   `user:<login>` (other metadata keys a project lists in `graph_metadata_keys`:
   `metadata:ACME/<key>/<value>`). It lists the members; `depth=2` with
   `types=["targets_version", "assigned_to", "tagged", "in_component"]` adds their assignees, tags and
   components. Away from the root these nodes are leaves with `degree` (items you may see that point
   at them); naming their kind in `types` expands them. From such a node `depth=3` is allowed, so
   `version:ACME/1.2` with `depth=3, types=["tagged"]` gives the issues, their tags and the pages
   with those tags in one call.
7. Narrow the result with `filter`: `specivo_get_links("version:ACME/1.2",
   filter={{"component": "importer", "status": "open"}})` keeps only the matching issues.
   Keys: `status` (`open`, `closed` or a status name), `tracker`, `version`, `assignee` (a login or
   `me`), `component` or another key in `graph_metadata_keys`; a list value means any of them. The
   filter runs after the read: the root is always kept, wiki pages are dropped, and tags, versions and
   the like stay while linked to a kept issue.
8. How are two things connected? `specivo_get_links("ACME-12", to="wiki:ACME/<slug>")` returns the
   shortest path (at most 4 hops, only through items you may see) as ordered `nodes` and `edges`, or
   `path: null` with `reason` = `no_path_within_4_hops` or `budget_exhausted`. It does not pass through
   tags, versions and other structured nodes unless `types` names their kind.
9. Save tokens with `format="compact"`: one line per edge, `ACME-12 -wiki_link-> wiki:ACME/runbook
   "Runbook"`, titles written once, after a header line with the root and whether it was truncated.
10. Suggested links: `specivo_show_issue` and `specivo_read_wiki` end with `Possibly related (suggested,
   not linked):` -- up to 5 issues or pages of the same project whose content is close to this one
   (cosine score in brackets) and that nothing links yet. They are hints from embeddings, not links:
   read them before relying on them. `specivo_get_links(ref, include_suggestions=true)` (or
   `types=["similar"]`) returns them as edges of kind `similar` with `suggested: true` and the score in
   `label`; they are never returned otherwise. One that is right:
   `specivo_link_suggestion(op="accept", ref, other_ref, link_type)`. Two issues get a relation
   (`link_type` is the relation type, default `relates`); an issue and a page, or two pages, get a typed
   link from `ref` to `other_ref` (`documents`, `implements`, `decided_in`, `supersedes` or `see_also`,
   the default), shown as kind `typed_link`. A wrong one: `op="dismiss"`, and it is never suggested
   again. Both need the permission that makes the link (`manage_issue_relations` between issues, else
   `edit_issues` / `manage_wiki` on `ref`).

### Connecting issues and pages (the link graph)
- Issue to issue: add a relation with `specivo_add_relation` (`blocks`, `relates`, `duplicates`, ...)
  or a parent with `parent_ref`. These are typed, so prefer them to mentioning the other issue in text.
- In prose, write `ACME-42` or `[[Page title]]` only where the text naturally refers to that issue or
  page. A Markdown link to an issue, wiki page or attachment URL of this instance also counts (use it
  for another project's wiki page). A key inside code is not a link.
- Do not add references to text just to connect things; `specivo_get_links` already follows
  relations, parents, children and attachments.

### Search modes
- `mode="hybrid"` *(default)* -- FTS + pgvector semantic via RRF fusion.
  Best general-purpose recall. Use this unless you have a reason not to.
- `mode="keyword"` -- tsvector FTS only. Fastest; best for literal strings
  when semantic recall adds noise.
- `mode="semantic"` -- pgvector embeddings only. Best for conceptual queries
  where wording varies (requires embeddings to be populated).
- Issue references (`ACME-42`) work in any mode: the issue itself comes
  first, followed by issues, comments and wiki pages that mention it.
- `limit` is clamped to 1-50; the tool has no offset.
- `expand=1` lists under each of the top 5 issue and wiki page hits what it is linked to (links,
  backlinks, relations, parent, children, tags, version, assignee; at most 10 each, only what you
  may see), so one call gives the answer and its context. The REST search takes `expand=1` too and
  returns them as `links` on those hits.

### Create an issue
1. `specivo_list_lookups()` -- get tracker_id, priority_id
2. `specivo_list_members(project_key)` -- get assigned_to_id
3. `specivo_create_issue(project_key, tracker_id, subject, ...)`

### Sub-issues (parent and children)
- `specivo_create_issue(..., parent_ref="ACME-12")` -- create a child of `ACME-12`
- `specivo_update_issue(issue_ref, parent_ref="ACME-7")` -- re-parent; `parent_ref=""` makes it top-level;
  omit `parent_ref` to leave the parent alone
- `specivo_list_issues(project_key, parent_ref="ACME-12")` -- the direct children
- Use a parent for "part of" structure, not a `relates` relation. Cycles and the depth limit are refused
  with an error and nothing changes.

### Tags
Tags are flat labels shared by a project's issues and wiki pages; agents and people filter by them.
1. `specivo_list_tags(project_key)` -- read the existing vocabulary first and reuse its names:
   `add` creates any name it does not know, so a typo or a synonym becomes a new tag.
2. Use a few short, lowercase, hyphenated tags (`backend`, `needs-review`), not sentences.
3. Tag at creation: `specivo_create_issue(..., tags=["backend"])` or
   `specivo_create_wiki(..., tags=["runbook"])` -- one call, like `metadata`.
4. Tag later: `specivo_tag("ACME-12", op="add", value=["backend", "needs-review"])`, or
   `specivo_tag("wiki:ACME/<slug>", op="add", value="runbook")` for a wiki page.
   `op="remove"` detaches, `op="set"` replaces the full set, `op="get"` reads them.

### Update issue when done
1. `specivo_list_lookups()` -- get status_id for resolved/closed
2. `specivo_update_issue(issue_ref, status_id=N, done_ratio=100, notes="...")`
3. `specivo_log_time(project_key, hours, activity_id, issue_ref)`

### Time work with the timer
1. `specivo_timer(op="start", issue_ref="ACME-12")` -- one timer per user; a running one is stopped
   and logged first (the reply gives its entry ID)
2. `specivo_timer(op="get")` -- what runs and for how long
3. `specivo_timer(op="stop", activity="Development", comments="...")` -- logs the time (activity name
   or ID; the default activity when omitted), or `op="discard"` to drop it without logging

### Log AI usage
You cannot see your own token counters. Never estimate tokens: log only numbers that come from a
hook, the Specivo reporter, or the tool's own usage output (Claude Code's `/cost`, for example).
The reporter (`POST /api/v1/ai-usage/ingest/`) is the routine way; the tools are for one-off entries
and corrections.
1. `specivo_log_ai_usage(tool="claude-code", model="claude-sonnet-4-5", input_tokens=N, output_tokens=N,
   cache_read_tokens=N, issue_ref="ACME-12")` -- without `issue_ref` / `project_key` the entry goes to
   the issue this agent session works on, else stays unassigned; the server prices it. A client that
   may retry passes `idempotency_key` (plus `started_at` / `ended_at` with an offset): the same key
   again replaces the entry's totals instead of counting twice
2. `specivo_ai_usage(op="list")` -- the entries logged in your name (unassigned ones included);
   `project_key="ACME"` for a project's entries you may read. Your own entries show the agent
   session they came from (`session=`); `session_id="..."` lists just that session's entries, to
   check which issue each part of a session went to
3. `specivo_ai_usage(op="update", entry_id=N, issue_ref="ACME-12")` -- attribute or correct an entry;
   entries from the reporter only take a project, issue and comments
4. `specivo_ai_usage(op="exclude", entry_id=N)` -- leave an entry out of reports (`include` undoes it)
5. `specivo_ai_usage_report(project_key="ACME", group_by="user,model", preset="last_month")` -- spend and
   API-equivalent value per group in the reporting currency; `period="week"` adds buckets, no
   `project_key` reports the whole instance (administrators). Spend includes seat cost (subscription
   fees spread over the entries that used them) when you read every entry of the project.
   `issue_ref="ACME-12"` (with `include_sub_issues=true` for the issues below it) totals one issue,
   e.g. for the spend line of a closing note; `release="1.0"` or `sprint="Sprint 4"` totals one release or
   sprint (an issue counts towards its own, else its nearest parent's), and `group_by="version"` or
   `"sprint"` compares them

### Link your client's sessions
A client with its own session ids (Hermes, for example) names them, so AI usage the reporter sends with
the same `session_id` and events from its webhook join the right agent session by id:
1. `specivo_agent_session(op="set", external_session_id="sess_abc", parent_session_id="sess_root")`
   -- once per session, with `issue_ref="ACME-12"` when the issue is already known
2. Several sessions over one MCP connection (subagents, gateway chats): call `set` with the session's
   own `external_session_id` before the work of each turn. Each id gets an agent session of its own on
   the connection; the one named by the last `set` is current, so the connection's later tool calls,
   issue changes and audit rows count towards it. Calls that run at the same time on one connection
   cannot be told apart: give such sessions a connection each
3. `specivo_agent_session(op="set", external_session_id="sess_abc", issue_ref="")` -- forget the issue
   when the session stops working on it (done, or switched by hand); a change is audited
4. `specivo_agent_session(op="get", external_session_id="sess_abc")` -- that session's issue and ids
   (without `external_session_id`: the current one)

### Register AI subscriptions from invoices
When a person hands you their AI plan invoices (Claude, ChatGPT, Cursor, Copilot, Google AI):
1. `specivo_ai_subscriptions(op="presets")` -- map each invoice to a `plan_key` (`custom` for any
   other plan, with `provider` and `plan_name`)
2. `specivo_ai_subscriptions(op="list")` -- see what is registered already, so you do not add it twice
3. `specivo_ai_subscriptions(op="add", periods=[{{"plan_key": "claude-max-5x", "amount": "100",
   "currency": "USD", "starts_on": "2026-01-21", "ends_on": "2026-02-25"}}, ...])` -- one period per
   plan and price, all in one call: the amount actually charged, tax included, in its currency, as a
   string; `ends_on` on every period that ended, none on the one that runs. Days are UTC and both
   ends count. All or nothing: if any period is invalid, nothing is added and every problem is listed
4. Read the warnings: a gap or overlap between periods of one plan family (same provider and tools)
   is reported, never refused. Ask the person about a gap (a cancelled month?) rather than filling it
5. `op="end"` (`subscription_id`, `ends_on`) when a plan is cancelled; `op="update"` to correct one.
   Prefer `end` to `delete`: deleting removes the plan from past spend reports
Needs an unlimited personal API key: a project-limited key and a service account are refused, since
subscriptions belong to a person. Administrators pass `user="login"` for someone else's.

### Register a company seat plan (administrators)
A Team or Enterprise plan the employer pays for, shared by several people:
1. `specivo_ai_subscriptions(op="add", company=true, periods=[{{"plan_name": "Claude Team",
   "provider": "anthropic", "amount": "750", "currency": "USD", "seat_count": 25,
   "starts_on": "2026-11-01"}}])` -- the fee of the whole plan; it may be `"0"` when its cost is
   booked elsewhere. It gets a company billing account paid by the employer
2. `op="add_member"` (`subscription_id`, `user="login"`, `starts_on`) for each person with a seat;
   their usage of those days is charged to the plan, before their own subscriptions
3. `op="end_member"` (`subscription_id`, `user` or `member_id`, `ends_on`) when someone leaves: the
   months before keep their seat cost. `op="remove_member"` only for a seat entered by mistake
4. `op="members"` lists the seats; `op="list", company=true` the plans
Each member carries one seat's share of the fee (fee / seats, by the days they held it); seats nobody
held show as unallocated seat cost in the instance report. Seat cost of a company plan is never
reimbursable.

### Register AI billing accounts
Billing accounts say what usage was paid through and who pays (employer or employee):
1. `specivo_ai_billing_accounts(op="list")` -- the company accounts and your own (each subscription
   is the fee schedule of one of yours)
2. A company gateway (administrators): `specivo_ai_billing_accounts(op="create", owner="instance",
   slug="corp-gateway", name="Company gateway", kind="gateway", payer="employer",
   route_patterns=["corp-*"], default_cost_type="api")` -- usage whose route matches is paid through it
3. A personal API key used for work: `op="create"`, `kind="api_key"`, `payer="employee"`,
   `reimburse_share="0.5"` for the part your employer pays back
4. `op="update"` (`account_id` plus fields) to change one; `op="deactivate"` to switch one off (accounts
   are never deleted; `is_active=true` switches one on again)
Changing an account's payer, patterns, tools, default cost type or state re-resolves the usage it
affects. A project's payer default (employer, employee or none) is a project setting
(`ai_usage_payer_default`), used when no account decides.

### Claim back AI usage you paid for
An employee-paid account with a `reimburse_share` makes its usage in company projects (payer default
`employer`) reimbursable: share x (seat cost + marginal spend).
1. `specivo_ai_reimbursement_claims(op="create", month="2026-03")` -- a draft for a month that has ended,
   priced from that month's usage; `op="get"` shows its entries, `op="refresh"` takes the current figures
2. `op="submit"` (`claim_id`) -- the amounts are frozen from now on; administrators are mailed
3. An administrator: `op="list", user="all", status="submitted"`, then `op="approve"`, `op="reject"`
   (`reason`) or, once the money is sent, `op="paid"` -- never on their own claim
4. `op="export"` returns the claim as CSV
An entry edited, re-priced, re-billed, excluded or deleted after submission is reported as CHANGED;
the claim keeps its amount. `op="withdraw"` takes a submitted claim back to draft.

### Budget AI spend
A monthly budget on a billing account, or on everything one payer pays, mails administrators and the
account holder when this month's spend reaches a threshold (once per threshold and month):
1. `specivo_ai_spend_budgets(op="create", billing_account="corp-gateway", amount="2000", currency="USD")`
   -- alerts at 80% and 100% unless you pass `thresholds=[50, 80, 100]`
2. Administrators: `op="create", payer="employer", amount="5000", currency="USD"` for the company's
   whole AI spend
3. `op="list"` shows each budget with this month's spend, percent and the alerts already sent;
   `op="update"` (`budget_id` plus `amount`, `currency`, `thresholds` or `is_active`) and `op="delete"`
Spend is the spend report's (marginal spend plus seat cost) for the calendar month so far.

### Patch description or wiki
1. `specivo_show_issue(issue_ref, search="keyword")` -- find exact text
2. `specivo_edit_description(issue_ref, search_text, replace_text)`

### Build large wiki pages incrementally
1. `specivo_create_wiki(project_key, title, text)` -- initial content
2. `specivo_append_wiki(project_key, slug, text)` -- add sections at end
3. `specivo_append_wiki(project_key, slug, text, position="after:## Heading")` -- insert after heading

### Read and update specific wiki sections
1. `specivo_read_wiki_section(project_key, slug, heading="## Design")` -- read one section
2. `specivo_replace_wiki_section(project_key, slug, heading="## Design", text="...")` -- replace body

### Diagrams (Mermaid)
Issue descriptions, comments and wiki pages are Markdown, and a fenced block with the `mermaid`
language renders as a diagram (flowchart, sequence, class, state, ER, Gantt and the other Mermaid
types). Use it instead of ASCII art for architecture, flows and state machines:

````
```mermaid
flowchart LR
    Push[git push] --> Hook[Specivo webhook] --> Issue[ACME-12 moves to Resolved]
```
````

Diagrams render with Mermaid's strict security level: HTML labels, `click` callbacks and
`%%{{init}}%%` overrides of security or theme settings are ignored, so keep labels plain text. A
diagram that fails to parse shows its source and an error instead, so check the syntax. The source
stays in the text, so search, `specivo_read_wiki` and `specivo_show_issue` return it unchanged.

### Filter issues by assignee, tag or release
`specivo_list_issues` narrows like the issue list's filters:
- `assignee="alex"` (login or user id), `assignee="me"`, `assignee="none"` (unassigned)
- `tag="frontend"` (name, case-insensitive, or tag id)
- `release="0.13.0"` (version name or id; versions shared with the project count), `release="none"`

An unknown name or id is an error naming the argument, never an empty list.

### Filter issues by metadata
`specivo_list_issues` accepts `metadata_filters=["key=value", ...]`. Pairs are
AND-combined. Each pair matches when the issue's metadata satisfies either:
- scalar equality: `issue_metadata->>'key' = 'value'`, OR
- array containment: `issue_metadata->'key' @> '["value"]'` (the array stored
  at *key* contains *value*).

So `metadata_filters=["component=frontend"]` matches both
`component: "frontend"` and `component: ["frontend", "backend"]`. Discover
available keys with `specivo_list_metadata_schemas(project_key)`.

## File uploads (attachments)

MCP tools work with text. For binary file uploads (images, PDFs, etc.), use the REST API directly via shell.

**Step 1**: Get the issue/page numeric ID:
```
specivo_show_issue(issue_ref="ACME-12", metadata_only=true)  # note the id field
```

**Step 2**: Upload via curl (works on macOS, Linux, Windows 10+):
```bash
curl -X POST https://YOUR-SPECIVO-HOST/api/v1/attachments/ \\
  -H "Authorization: Bearer spv_YOUR_API_KEY" \\
  -F "file=@/path/to/file.png" \\
  -F "container_type=Issue" \\
  -F "container_id=ID"
```

For wiki pages use `container_type=WikiPage`. On Windows use `curl.exe` instead of `curl`.

Reuse the API key (`spv_...`) and host from your MCP connection config.
Max file size is configured per instance (nginx `client_max_body_size`).

## REST API authentication

When you need to call the Specivo REST API directly (e.g. file uploads), reuse the
API key and host from your MCP connection config. The key is the `spv_...` token
in your `Authorization` header, and the host is the base URL of the MCP SSE endpoint.

Specivo uses **only** `Authorization: Bearer spv_...` for API authentication.
Other methods (X-Api-Key, query params, etc.) are NOT supported.
All API URLs require a trailing slash.

```bash
curl -H "Authorization: Bearer spv_YOUR_API_KEY" \\
  https://YOUR-SPECIVO-HOST/api/v1/projects/ACME/wiki/
```

## Permissions and audit log

All write tools enforce the underlying project permission and write a row to
the security audit log. Notable cases:

- `specivo_create_metadata_schema`, `specivo_update_metadata_schema`,
  `specivo_delete_metadata_schema` -- require **`manage_project`** on the
  target project. Each call emits a `METADATA_SCHEMA_CREATED/UPDATED/DELETED`
  audit event tagged with `source=mcp`.
- Issue and wiki mutations require the matching `add_*` / `edit_*`
  permission and emit `ISSUE_*` / `WIKI_*` audit events.
- `specivo_log_time` and `specivo_timer` (`start`, `stop`) require **`log_time`** on the project.
  Reading logged time needs **`view_time_entries`**; the seeded Agent role has both.
- `specivo_log_ai_usage` requires **`log_ai_usage`** on the project (nothing for unassigned usage in
  your own name); `on_behalf_of` requires **`log_ai_usage_for_others`** and an active member as the
  target, and gives the same refusal for every login otherwise. `specivo_ai_usage` changes entries
  logged in your name, or any with **`manage_ai_usage`**. Everyone reads what is logged in their
  name; others' entries need **`view_ai_usage`** with visibility `all`.
- `specivo_ai_usage_report` reads only the entries you may read; the instance report (no
  `project_key`) is for administrators. Seat cost shows to administrators and to whoever reads every
  entry of the project in the period; plan names never appear.
- `specivo_ai_subscriptions` works on the key owner's own subscriptions, and only with an unlimited
  key of a person (not a service account); changing them needs self-service on. Administrators reach
  anyone's (`user=`), and their changes to somebody else's write `ai_subscription_*` events.
  Company seat plans and their members are for administrators only; every change to them writes
  `ai_subscription_*` / `ai_subscription_member_*` events. Every change over MCP writes
  `ai_subscriptions_changed`.
- `specivo_ai_billing_accounts` needs an unlimited key. Everyone lists the instance accounts and
  their own; company accounts (`owner="instance"`) and other people's (`owner="login"`) are changed
  by administrators only, and those changes write `ai_billing_account_*` events. Changing your own
  needs self-service on. Every change over MCP writes `ai_billing_accounts_changed`.
- `specivo_ai_reimbursement_claims` needs an unlimited key of a person. You see and change your own
  claims; administrators see everyone's and approve, reject or mark paid somebody else's. Each step
  writes an `ai_reimbursement_claim_*` event, and every change over MCP also writes
  `ai_reimbursement_claims_changed`.
- `specivo_ai_spend_budgets` needs an unlimited key. Administrators see and change every budget;
  anyone else sees and changes the budgets on their own accounts (with self-service on). Budgets on company
  accounts, on a payer and administrators' changes to somebody else's write `ai_spend_budget_*`
  events; every change over MCP writes `ai_spend_budgets_changed`.
- `specivo_create_issue` follows the tracker's private-by-default setting
  unless `is_private` is passed, which needs **`set_issues_private`**.

If a tool returns a permission error, the caller's API key user lacks the
required role on that project. Audit rows are visible to project managers
in the project security log.

## Tool hints

Every tool declares `readOnlyHint`, `destructiveHint`, `idempotentHint` and
`openWorldHint` explicitly, and a test keeps them complete, so clients may rely
on them for approvals and retries:

- **readOnlyHint: true** -- lists, shows, reads, search, `specivo_whoami`,
  `specivo_setup_guide` and `specivo_ai_usage_report`. Safe to retry after a
  dropped session.
- **destructiveHint: true** -- deletes, `specivo_remove_relation`,
  `specivo_complete_sprint`, `specivo_skip_recurrence_occurrence`,
  `specivo_ai_subscriptions` (its `delete` and `remove_member` ops),
  `specivo_ai_reimbursement_claims` and `specivo_ai_spend_budgets` (their `delete` ops).
- **idempotentHint: true** -- updates by key, deletes, `specivo_add_relation`,
  `specivo_tag` and `specivo_ai_usage`: a repeat with the same arguments
  changes nothing more.
- Creates, comments, appends, `specivo_edit_description`, `specivo_edit_wiki`,
  `specivo_log_time`, `specivo_log_ai_usage`, `specivo_timer`,
  `specivo_metadata` and `specivo_ai_billing_accounts` are not idempotent: do not blindly retry them after a
  timeout; check first whether the first call went through.
- **openWorldHint** is false everywhere: every tool acts on this instance only.
  Tools with an `op` argument carry the hints of their most far-reaching op.

## Anti-patterns

- Do NOT pass numeric IDs for `issue_ref` -- always use display key format `ACME-12`.
- Do NOT assume tracker/status/priority IDs -- call `specivo_list_lookups` first.
- Do NOT replace full description via `update_issue` -- use `edit_description` for patches.
- Do NOT call `log_time` without a valid `activity_id` from `list_lookups`.
- Do NOT invent or estimate token counts for `specivo_log_ai_usage` -- you cannot see them; use a
  hook, the reporter or `/cost` output.
- Do NOT use `X-Api-Key` or `?key=` for REST API auth -- only `Authorization: Bearer spv_...` works.
- Do NOT omit the trailing slash on REST API URLs -- FastAPI requires it.
- Do NOT write tags or other tracked properties (status, assignee, metadata) into an issue description
  or wiki page -- no YAML frontmatter, no `> **Tags**: ...` line. Use `tags=` / `specivo_tag`,
  `specivo_update_issue` and `metadata=` / `specivo_metadata`; text in the body is not searchable as
  a property and goes stale.
{footer}"""


def _format_header(fmt: str) -> str:
    """Return format-specific header."""
    summary = "Specivo -- self-hosted platform for project tracking, knowledge base, and AI-safe automation."
    if fmt == "claude":
        return f"""# Specivo MCP -- Agent Setup Guide

> {summary}

Add to your CLAUDE.md or project instructions."""
    if fmt == "cursor":
        return f"""# Specivo MCP Rules

> {summary}

Add to your .cursorrules file."""
    return f"""# Specivo MCP -- Agent Setup Guide

> {summary}"""


def _format_footer(fmt: str) -> str:
    """Return format-specific footer."""
    if fmt == "claude":
        return """
## Connection

### Claude Code / Cursor / Windsurf / Cline (JSON, native SSE)

```json
{
  "mcpServers": {
    "specivo": {
      "type": "sse",
      "url": "https://your-specivo-host/mcp/sse/",
      "headers": { "Authorization": "Bearer spv_your_api_key_here" }
    }
  }
}
```

### Codex CLI (TOML, native Streamable HTTP)

Codex CLI supports Streamable HTTP natively. Add to `~/.codex/config.toml`:

```toml
[mcp_servers.specivo]
url = "https://your-specivo-host/mcp/"
bearer_token_env_var = "SPECIVO_API_KEY"
```

Then export the API key in your shell before running `codex`:

```bash
export SPECIVO_API_KEY=spv_your_api_key_here
```

Codex will send `Authorization: Bearer $SPECIVO_API_KEY` on every request.
No `mcp-remote` bridge needed — connects straight to `/mcp/`.
"""
    return ""


def _build_tool_table(mcp_server: MCPServer | None = None) -> str:
    """Build markdown table rows from registered tools or fallback summaries."""
    summaries = dict(_TOOL_SUMMARIES)

    # Merge any tools from the actual server that are not in our static map
    if mcp_server is not None:
        try:
            for tool in mcp_server._tool_manager.list_tools():
                if tool.name not in summaries:
                    desc = tool.description or ""
                    summaries[tool.name] = desc.split("\n")[0][:80]
        except Exception:  # noqa: BLE001, S110
            pass

    rows: list[str] = []
    for name, desc in summaries.items():
        rows.append(f"| `{name}` | {desc} |")
    return "\n".join(rows)
