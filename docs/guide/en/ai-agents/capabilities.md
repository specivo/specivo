---
description: What an AI agent can do through the Specivo MCP server — the tool catalog grouped by area, read vs write actions, permission enforcement, and the audit log.
---

# What agents can do

Through the MCP server, an agent can reach most of what you'd do in the Specivo UI. The server
exposes a catalog of around 70 tools, all prefixed `specivo_`. You rarely call them by name — you
describe what you want, and the client picks the right tool. This page groups them by area so you
know the shape of what's possible. A connection loads the `core` toolset unless its URL asks for
more: planning, recurring tasks, tag and schema management and AI spend are opt-in
[toolsets](connecting.md#choose-toolsets).

## The tool catalog by area

| Area | What the agent can do |
|---|---|
| **Issues & comments** | List issues, narrowed like the issue list's filters (status, assignee — a login, `me` or `none` —, tag, release — a version name or `none` —, sprint, parent and metadata values); read, create, and update issues; edit descriptions and core fields; set or change an issue's [parent](../issues/subtasks.md) and list its subtasks; [move an issue to another project](../issues/updating.md#move-an-issue-to-another-project); add comments and read the comment history, or the whole change history with the names of the statuses, people and versions a change refers to. |
| **Search** | Search across issues, wiki, and more using Specivo's hybrid (keyword + meaning) search. With `expand=1`, each of the top 5 issue and wiki page hits also lists up to 10 things it is linked to (links, backlinks, relations, parent, children, tags, version, assignee), one line per link, so the agent sees a hit's context without a second call. Comments and attachments are not expanded. |
| **Wiki** | List, read, create, edit, and append to wiki pages, including section-level reads and edits, and history/restore. |
| **Metadata & schemas** | Read and set [issue metadata](../metadata/values.md) values, and list the available metadata schemas. |
| **Tags** | List a project's [tags](../projects/tags.md), tag issues and wiki pages (also while creating them), and, with **Manage project**, create, rename, recolor and delete tags. |
| **Relations** | Link issues with typed [relations](../issues/relations.md) — relates, blocks, duplicates, and so on — and remove them. |
| **Links** | Follow the links between items: `specivo_show_issue` and `specivo_read_wiki` list what an item links to and what links to it (backlinks), and `specivo_get_links` returns the neighbourhood as JSON. See [Navigating the link graph](#navigating-the-link-graph). Agents only see links between items the key may open. |
| **Development** | `specivo_issue_development(issue_ref, kinds=[...], limit, offset)` returns an issue's [development activity](../integrations/repositories.md#reading-development-activity-through-the-api-and-mcp) as JSON, the same document as the REST API: commits, pull requests, reviews, CI runs, deployments with their environment type, and releases, with a summary and a paged timeline. `specivo_show_issue` sums it up on one `Development:` line. Agents see it only for issues the key may open. |
| **Versions** | List and manage versions (milestones/releases) and see which issues belong to each. |
| **Sprints** | List sprints, see sprint issues, and manage the plan/start/complete lifecycle. |
| **Recurring tasks** | Create, list, update, and delete [recurring task patterns](../projects/recurring-tasks.md); skip a single scheduled occurrence; preview upcoming occurrences. The template subject and description support date macros (`{{month}}`, `{{weekday}}`, …) that expand per occurrence in the pattern's timezone. |
| **Time** | Log time against an issue or project with an activity, or run your own timer: start it on an issue, check it, stop it to log the time, or discard it. |
| **AI usage** | `specivo_log_ai_usage` logs the tokens an AI tool used on an issue or project, and `specivo_ai_usage` lists, reads, corrects, excludes and includes [AI usage entries](reporting-ai-usage.md#read-correct-and-exclude-entries). Each of the agent's own entries names the agent session it came from, and `session_id` lists one session's entries in every project, so an agent can check where its usage went. `specivo_ai_usage_report` totals spend like the [AI spend reports](../projects/ai-spend-reports.md); with `issue_ref` it covers one issue, and `include_sub_issues=true` adds the issues below it. Agents cannot see their own token counters, so the numbers must come from a hook, the [reporter](reporting-ai-usage.md) or the tool's own `/cost` output, never from an estimate. Without an issue or project, an entry goes to the issue the agent session works on (see [Agent sessions](#agent-sessions)), or stays unassigned. |
| **AI subscriptions** | `specivo_ai_subscriptions` lists, adds, corrects, ends and deletes the [AI subscriptions](../install/ai-subscriptions.md#with-your-ai-agent) of the person whose key the agent uses, for example from their invoices, several plan periods in one call. It warns about overlaps and gaps between periods of one plan, and refuses a project-limited key and a service account. |
| **Orientation** | `specivo_whoami` (which account the key is), `specivo_list_lookups` (tracker/status/priority IDs), and `specivo_setup_guide` (config help). |

!!! tip "Start with orientation tools"
    A well-behaved agent calls `specivo_list_lookups` to learn your project's tracker, status, and
    priority IDs before creating or updating issues, and `specivo_whoami` to confirm who it's acting
    as. If you connect a new agent, ask it to run these first.

## Navigating the link graph

Specivo keeps a graph of how issues and wiki pages are connected: text links (`ACME-42`, `[[Page]]`,
links to this instance's pages), relations, parent and children, commits and pull requests, and the
things items share. Tags, versions, sprints, assignees and the values of some metadata keys (by
default `component`) are **nodes** of their own, so an agent can ask "what is in this release?" and
get the issues, not just text matches.

`specivo_get_links` takes a **ref** to start from:

| Ref | Starts from |
|---|---|
| `ACME-12` | an issue |
| `wiki:ACME/<slug>` | a wiki page |
| `version:ACME/<name>` | a version and the issues planned for it |
| `sprint:ACME/<name>` | a sprint and its issues |
| `tag:ACME/<name>` | a tag and the issues and pages that carry it |
| `component:ACME/<value>` | a component (the `component` metadata value) |
| `metadata:ACME/<key>/<value>` | the value of another metadata key your project lists (see below) |
| `user:<login>` | a person and the issues assigned to them |
| `concept:ACME/<name>` | a concept extracted by the project's AI provider, and what mentions it (see below) |

From a tag, version, sprint, component, metadata value or person, the result lists the members,
newest first, up to `limit`. Anywhere else these nodes are leaves with a `degree`, the number of
items you may see that point at them. Hand-entered commit SHAs in an issue's `commits` metadata
become commit nodes too.

The options:

| Option | What it does |
|---|---|
| `direction` | `in` (what points here, or a node's members), `out` or `both` (default). |
| `depth` | `1` (default) or `2`, which expands the linked issues and pages once more. From a tag, version, sprint, component, metadata value or person, `3` is allowed, for example a version, its issues, their tags and the pages with those tags. |
| `types` | Edge kinds to keep. Text links: `wiki_link`, `mention`, `attachment_link`. Typed links (accepted suggestions): `typed_link`. Structured: `relation`, `parent`, `child`, `attachment`, `commit`, `pull_request`. Nodes: `tagged`, `in_component`, `in_metadata`, `targets_version`, `in_sprint`, `assigned_to`, `mentions` (concepts; `concept` works too). Suggested links: `similar` (never returned unless named). Naming a node kind also expands those nodes at depth 2. |
| `include_suggestions` | `true` adds suggested links (kind `similar`) to the kinds read, and the concept mentions below the project's confidence threshold. Off by default. |
| `limit` | Neighbours at most, 1 to 200 (default 50). `truncated` says when more were left out. |
| `to` | Another ref. Returns the shortest path between the two instead of the neighbours (see below). |
| `filter` | Keeps only the issues that match, for example `{"status": "open", "component": "importer"}` (see below). |
| `format` | `json` (default) or `compact`. |

Attachments are not listed as nodes by default. Each issue and page node has an `attachments` count
instead, and `types=["attachment"]` lists them.

**How are two things connected?** `specivo_get_links("ACME-12", to="wiki:ACME/runbook")` returns the
shortest path, at most 4 hops, over links the key may see. It steps through issues and wiki pages
over text links, relations and parent/child links, and never through an item the key cannot see.
Tags, versions and other nodes are passed through only when `types` names their kind, but either end
of the path may be one. The answer is a `path` with `hops`, ordered `nodes` and `edges`, or
`path: null` with a `reason`: `no_path_within_4_hops`, or `budget_exhausted` when the search stopped
after 500 nodes. `to` cannot be combined with `filter`.

**Filtering.** `filter` keys are `status` (`open`, `closed` or a status name), `tracker`, `version`,
`assignee` (a login, or `me`), `component`, and any other key in the project's list of metadata keys.
A list value means any of them; matching ignores case; an unknown key is an error. The filter runs
after the read, which then reads up to 200 nodes and cuts the result to `limit`. The root is always
kept, wiki pages are dropped, and tags, versions and other nodes stay while they are linked to a
kept issue.

**Compact output.** `format="compact"` saves tokens. It starts with a header such as
`root: ACME-12 "Import fails" [In Progress] (14 nodes, 17 edges)` (plus `, truncated` when
applicable), then one line per edge, `ACME-12 -wiki_link-> wiki:ACME/runbook "Runbook"`. A node's
title is written once, after its ref.

**Suggested links.** `specivo_show_issue` and `specivo_read_wiki` end with a line `Possibly related
(suggested, not linked):` listing up to five issues or pages of the same project whose content is
close (with a score from 0 to 1) and that nothing links yet. `specivo_get_links` returns them only
when asked: `include_suggestions=true` or `types=["similar"]` adds edges of kind `similar`, with
`suggested: true` and the score in `label`. They are hints from the embeddings, not links.
`specivo_link_suggestion(op="accept", ref, other_ref, link_type)` makes one a real link: a relation
between two issues (`link_type` is the relation type, `relates` by default), or a typed link from
`ref` to `other_ref` for an issue and a page or two pages (`documents`, `implements`, `decided_in`,
`supersedes` or `see_also`, the default), listed as kind `typed_link`. `op="dismiss"` hides the pair
for good. Both need the permission that makes the link, and both are in the audit log.

??? note "Which metadata keys become nodes"
    Project managers choose the keys in **Project settings → General → Metadata keys in the link
    graph**: a comma-separated list of at most 10 [metadata](../metadata/index.md) keys, `component`
    by default. Every value of those keys becomes a node. Leave the field empty to turn metadata nodes
    off. The change takes effect for the next read; no rebuild is needed.

??? note "What a key can see"
    Everything is filtered for the key's user. Tags are visible to anyone who may view the project's
    issues or wiki. Versions, sprints and metadata values need permission to view the project's
    issues. A person node appears only for signed-in users, and `user:<login>` works only for
    yourself, an administrator, or a member of a project whose issues you can view.

### Concepts

When a project has [concept extraction](../projects/index.md#concept-extraction-ai) switched on, its
own AI provider lists the features, external systems, decisions and terms its issues and wiki pages
talk about. Each is a node `concept:ACME/<name>` (the name in lower case), with the concept's kind
as its `status`. An issue or page points at each concept it mentions with an edge of kind
`mentions`: `label` is the model's confidence (for example `0.85`) and `passage` the text it was
found in.

- `specivo_get_links("concept:ACME/billing gateway")` lists the issues and pages that mention it.
- `specivo_get_links("ACME-12", types=["mentions"])` lists the concepts of an issue. Without
  `types` they come as leaves with a `degree`, like tags.
- Mentions below the project's confidence threshold come only with `include_suggestions=true`,
  marked `suggested: true`.
- A path (`to=`) passes through a concept only when `types` names `mentions`.

Concepts are a model's reading of the text, not links anyone made: check the passage before relying
on one. You see a concept only when you may see an issue or page that mentions it.

## Read vs write

Roughly half the tools **read** (list, show, search, read wiki) and half **write** (create issues,
add comments, update status, log time or AI usage, edit wiki). The split matters because of permissions:

- **Reads** return whatever the key's account is allowed to see.
- **Writes** are checked against that account's project role *every time*. If the account can't make
  a change in the UI, the agent can't make it over MCP either.

This is why a [dedicated service account with the right role](api-keys.md) is the safe way to scope
an agent: give it read-only roles where it should only look, write roles only where it should act.

## Every write is audited

Each MCP tool call an agent makes, read or write, is recorded in the
[security audit log](../install/audit-log.md), tagged `source=mcp`, with the account that made it,
when, and the client address it came from. Administrators review it under **Admin → Audit log** to
see exactly what an agent did. Issue edits also show up in the normal issue history and comment
journal, the same as a person's edits — nothing an agent does is hidden.

The log covers MCP. An agent that calls the REST API with its key is treated like a person: its
issue and wiki edits are in their history, not in the audit log.

## Agent sessions

Specivo also groups each key's calls into **agent sessions**. Each MCP connection has sessions of
its own: when a client connects (a new Claude Code session, for example) it starts a new session
with no issue, even if another client uses the same key at the same time or stopped a minute ago.
A client that reconnects with its session id, after a network drop or a server restart, keeps its
session. Plain REST calls of a key share one session of their own. Calls of one connection less than
30 minutes apart (`AGENT_SESSION_TIMEOUT`, in seconds) belong to the same session; after a longer
pause a new one starts. A session records:

- **the client** that made the calls, by the name and version it announces when it connects (for
  example `claude-code` and `2.1.0`), or else the first product in its `User-Agent` header;
- **the issue the agent is working on**: over MCP, the issue it last created in an *in progress*
  status (any status in the active group) or assigned to its own account, moved into such a
  status, assigned to its own account, updated while it is in progress and assigned to its own
  account (so an agent resuming its issue marks it again, even when nothing changes), or started a
  timer on. The latest one wins. Only an issue the key's account can see is ever recorded.
- **how it was opened**: `mcp` (an MCP connection) or `rest` (plain REST calls);
- **the client's own session id and its parent's**, when the client reports them (see below).

An agent client that has session ids of its own (a Hermes session, for example) can report them with
`specivo_agent_session(op="set", external_session_id=..., parent_session_id=...)`, and can name its
working issue in the same call with `issue_ref`. The ids are 1 to 200 letters, digits and `. _ : -`.
The call changes only the session of the connection it is made on, and a parent is linked only to a
session of the same account, so another account's ids never attach to yours. When the
[reporter](reporting-ai-usage.md) later sends usage with the same `session_id`, Specivo attributes it
to that session's issue (or, when that session names none, to the nearest one in its parent chain)
instead of guessing from the time of the work. `specivo_agent_session(op="get")` shows the session.
Setting ids is recorded in the audit log.

A client that runs several conversations over one MCP connection (Hermes subagents or gateway chats,
for example) calls `specivo_agent_session(op="set", external_session_id=...)` before the work of each
one. Specivo selects, or creates, the session of that id on the connection, and the connection's later
calls count toward it; `op="get"` with an `external_session_id` shows that session. Tool calls that
run at the same time on one connection go to the session selected last, so give such sessions a
connection each when that matters. `specivo_agent_session(op="set", issue_ref="")` clears the
session's working issue; the change is audited.

One client conversation is one session, however its calls arrive:

- [Hermes events](hermes-events.md) with the same client session id join the account's open session. A
  run's end ends only a session the events opened, not the MCP session the conversation goes on in.
- A REST call with the header `X-Agent-Session-Id: <id>` joins the open session with that id, or
  starts a REST session that carries it. REST calls without the header never join such a session.
- A REST call with the header `X-Agent-Background: 1` is the client's own housekeeping, for example a
  sync that polls for new issues. It opens or extends no session and so does not keep one looking
  active. It is authenticated and audited as usual.

### The Agent sessions page

Every project has an **Agent sessions** page in its sidebar. It lists the sessions of the project's
member accounts, most recently active first: the **Account** (with a **Service account** badge for
agent accounts), the **Client**, the **Type** (MCP or REST), the **Issue** the session works on,
when it **Started**, its **Last activity** (with an **Active**, **Inactive** or **Ended** mark), the
number of **Tool calls**, and the **AI usage** linked to it (entries and spend you are allowed to
see). Filter by **Account** (a login), **Issue** (a key such as `ACME-12`) and **Show** (all, active
only, inactive only). A session working on an issue you cannot see is not listed.

Project managers (anyone with **Manage project**, and administrators) can open a session. Its page
shows the account and client, the client's session id and its **Parent session**, when it started,
was last active and ended, the **Related sessions** in its parent chain, and its **Tool calls**:
the MCP tools it ran, newest first, 20 per page, with the event and what each call named (project
keys, issue keys, page slugs). They come from the security audit log, so only that metadata is
shown, never what was sent to a tool or what it returned. Calls the session made in other projects
are left out, and so are calls without a project from tools that work in projects; calls of
`specivo_whoami`, `specivo_list_projects` and `specivo_list_lookups` are shown. **Parent session** and
**Related sessions** show only sessions you could open on this page. Below them is the AI
usage linked to the session (the newest 20 entries): the entries reported with its client session id, or, when it has none,
the entries of the same account whose start and end fall in the session's time. Opening a session
is recorded in the audit log.

When a section has more than 20 items, **Newer** and **Older** links and a page size (20, 50 or 100)
appear above it and load that section in place, without reloading the page. Without JavaScript the links
reload the page. The paging applies to **Tool calls** and to **AI usage** separately.

Tool calls made before this page existed are not linked to a session, so older sessions show none.

The same data is in the REST API: `GET /api/v1/projects/{key}/agent-sessions/` returns a paginated
list (`offset`, `limit`, and the filters `account`, `issue`, `active` and `external_session_id`),
and, for project managers, `GET .../agent-sessions/{id}/` returns one session with
`tool_call_count` and `usage_entry_count`, and `GET .../agent-sessions/{id}/events/` its tool calls.

### The Agents tab on an issue

On an issue, the **Agents** tab shows what agents did for it and what that cost: one block per agent
session that worked on the issue, newest first, 20 per page. It answers "what did my agent do
overnight, and what did it cost". Like a session's page, the tab is for project managers (anyone with
**Manage project**, and administrators), and only on issues they can see.

A session belongs to the issue when it worked on it, or when AI usage reported for the issue carries
its client session id. Each block starts at the top of the session's parent chain: the sessions above
it and its subagents are in the same block, the subagents listed under it. When one run is known to
Specivo twice, as an MCP connection and as [Hermes events](hermes-events.md) with the same client
session id, the two are shown as one session. An issue with sub-issues has a **With sub-issues** /
**This issue only** switch; sessions on sub-issues you cannot see are left out.

Each block shows:

- the **account** (with the **Service account** badge), the client, the type (MCP, REST, Webhook),
  the agent profile when its usage names one and an **unattended** mark for runs nobody watched;
- the **state**: **Running** (active in the last 30 minutes), **Completed**, **Interrupted** (Hermes
  said the run was interrupted) or **Idle**, with when it started, when it ended or was last active,
  and how long it took;
- the **counters**: tool calls, approvals asked and how they were answered, and subagents;
- the **AI cost** of the block: the spend and API-equivalent value, tokens and number of entries of
  the usage reported for its sessions, in the project's reporting currency, without seat cost. The
  line follows the [AI usage](../projects/ai-usage.md) rules: you need **View AI usage**, and you see
  only the entries your role lets you read. Until the reporter has sent the usage (every 30 minutes)
  the line says **Not reported yet**;
- the subagents, each with its state and a link to its own page;
- the **steps**: the newest 30 tool calls, approvals and subagent starts and stops of the session and
  its subagents, as on the session's page. **Show all** and **Open session** lead to that page.

Like the session pages, the tab shows only metadata (tool names, statuses, the keys a call named),
never what was sent to a tool or what it returned, and leaves out what the sessions did in other
projects. Opening the tab uses one AI report from your budget, as the **AI usage** tab does. The tab
also works as a page of its own, `/issue/{key}/agents/`.

The REST API has the same list: `GET /api/v1/issues/{key}/agent-sessions/` returns a paginated list
(`offset`, `limit` up to 50, `sub=true` to include sub-issues) with each session's ids, account,
client, state, counters, subagent ids and, for readers who may see AI usage, its `cost`.

The session's issue is a hint for attributing AI usage to an issue (`specivo_log_ai_usage` without
an issue or project uses it, when the account may log usage there); it grants nothing. Nothing about
the prompts or the conversation is stored.

## File uploads go through the REST API

The MCP tools are **text-only** — they can read and write issues, comments, and wiki text, but they
can't upload binary files. To attach a file, use Specivo's REST API with `curl`, reusing the same
`spv_…` key:

```bash
curl -X POST https://your-specivo-host/api/v1/attachments/ \
  -H "Authorization: Bearer spv_your_api_key_here" \
  -F "file=@./diagram.png" \
  -F "container_type=Issue" \
  -F "container_id=123"
```

`container_type` is `Issue`, `WikiPage` or `Journal` (a comment), and `container_id` is its numeric
id. Adding a file takes the right to change what it is attached to:

| Attached to | Permission needed |
|---|---|
| An issue | **Edit issues** or **Add comments**; or **Create issues** on an issue the account created |
| A wiki page | **Manage wiki pages** |
| A comment | **Edit any comments**, or **Edit own comments** on the account's own comment |

A file on an issue is recorded in the issue's history ("File diagram.png added"). To record it with
the edit or comment it belongs to, add `-F "journal_id=…"`: the `journal_id` returned by
`PATCH /api/v1/issues/{ref}/` (null when the edit changed nothing) or the `id` of the comment from
`POST /api/v1/issues/{ref}/journals/`. Each upload's response carries `journal_id` too, so further
files of the same batch can join the same entry.

!!! note "REST API basics"
    REST authentication is **only** the `Authorization: Bearer spv_…` header, and every API URL
    needs a **trailing slash**. The same key that connects your MCP client works here too.
