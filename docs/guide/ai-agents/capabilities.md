---
description: What an AI agent can do through the Specivo MCP server — the tool catalog grouped by area, read vs write actions, permission enforcement, and the audit log.
---

# What agents can do

Through the MCP server, an agent can reach most of what you'd do in the Specivo UI. The server
exposes a catalog of around 40 tools, all prefixed `specivo_`. You rarely call them by name — you
describe what you want, and the client picks the right tool. This page groups them by area so you
know the shape of what's possible.

## The tool catalog by area

| Area | What the agent can do |
|---|---|
| **Issues & comments** | List, read, create, and update issues; edit descriptions and core fields; [move an issue to another project](../issues/updating.md#move-an-issue-to-another-project); add comments and read the comment history. |
| **Search** | Search across issues, wiki, and more using Specivo's hybrid (keyword + meaning) search. |
| **Wiki** | List, read, create, edit, and append to wiki pages, including section-level reads and edits, and history/restore. |
| **Metadata & schemas** | Read and set [issue metadata](../metadata/values.md) values, and list the available metadata schemas. |
| **Relations** | Link issues with typed [relations](../issues/relations.md) — relates, blocks, duplicates, and so on — and remove them. |
| **Versions** | List and manage versions (milestones/releases) and see which issues belong to each. |
| **Sprints** | List sprints, see sprint issues, and manage the plan/start/complete lifecycle. |
| **Recurring tasks** | Create, list, update, and delete [recurring task patterns](../projects/recurring-tasks.md); skip a single scheduled occurrence; preview upcoming occurrences. The template subject and description support date macros (`{{month}}`, `{{weekday}}`, …) that expand per occurrence in the pattern's timezone. |
| **Time** | Log time against an issue or project with an activity. |
| **Orientation** | `specivo_whoami` (which account the key is), `specivo_list_lookups` (tracker/status/priority IDs), and `specivo_setup_guide` (config help). |

!!! tip "Start with orientation tools"
    A well-behaved agent calls `specivo_list_lookups` to learn your project's tracker, status, and
    priority IDs before creating or updating issues, and `specivo_whoami` to confirm who it's acting
    as. If you connect a new agent, ask it to run these first.

## Read vs write

Roughly half the tools **read** (list, show, search, read wiki) and half **write** (create issues,
add comments, update status, log time, edit wiki). The split matters because of permissions:

- **Reads** return whatever the key's account is allowed to see.
- **Writes** are checked against that account's project role *every time*. If the account can't make
  a change in the UI, the agent can't make it over MCP either.

This is why a [dedicated service account with the right role](api-keys.md) is the safe way to scope
an agent: give it read-only roles where it should only look, write roles only where it should act.

## Every write is audited

Each write an agent makes is recorded in the **security audit log**, tagged `source=mcp`, alongside
the account that made it and when. Project managers can review this log to see exactly what an agent
changed. Issue edits also show up in the normal issue history and comment journal, the same as a
person's edits — nothing an agent does is hidden.

## File uploads go through the REST API

The MCP tools are **text-only** — they can read and write issues, comments, and wiki text, but they
can't upload binary files. To attach a file, use Specivo's REST API with `curl`, reusing the same
`spv_…` key:

```bash
curl -X POST https://your-specivo-host/api/v1/attachments/ \
  -H "Authorization: Bearer spv_your_api_key_here" \
  -F "file=@./diagram.png"
```

!!! note "REST API basics"
    REST authentication is **only** the `Authorization: Bearer spv_…` header, and every API URL
    needs a **trailing slash**. The same key that connects your MCP client works here too.
