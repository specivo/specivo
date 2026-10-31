---
description: Practical things to ask a Specivo-connected AI agent to do — gather context, create issues, resolve and log time, append commit metadata, and build a wiki page.
---

# Common workflows

Once a client is [connected](connecting.md), you work with the agent by describing the outcome you
want — it picks the right tools. These are common patterns, framed as what you can ask your agent to
do. They assume the agent's account has the right role on the project; see
[API keys & scopes](api-keys.md).

## Gather context before working

The most useful habit is to have the agent orient itself before it acts. A good request chains:

1. **Search** for the topic across issues and wiki (Specivo's hybrid search finds matches by meaning,
   not just keywords).
2. **Read the issue** that looks relevant to get its full description, fields, and comments.
3. **Read the wiki** page it references for background or conventions.

> *"Find the open issues about the login flow, read the most relevant one, and check the wiki for
> our auth conventions before you suggest anything."*

This grounds the agent in your project's real state instead of guesses.

## Create an issue

Ask the agent to file work it (or you) discovered. It will look up the project's trackers and
priorities first, then create the issue with a clear subject and a Markdown description.

> *"Create a Bug in ACME: the export button does nothing on the reports page. Set priority to High
> and assign it to me."*

The new issue gets a reference like `ACME-42` you can use in follow-ups.

The create-issue tool also accepts a **metadata** map, so the agent can set custom
[metadata](../metadata/values.md) fields in the same call instead of creating the issue and then
updating it — for example a story-point estimate or a starting branch can go in at creation time.

## Move an issue to another project

When work was filed in the wrong project, the agent can move it without losing anything.

> *"Move ACME-42 to the HOME project with a note that it belongs to the home team."*

The issue keeps its history, comments, relations, attachments, watchers, time entries, and metadata,
and takes a new key in the target project; the old reference still resolves. Only standalone issues can
move — detach any parent or subtasks first. See
[Move an issue to another project](../issues/updating.md#move-an-issue-to-another-project) for the full
rules.

## Mark an issue resolved and log time

When a piece of work is done, the agent can update the status and record the effort in one go.

> *"Mark ACME-42 as Resolved, set it to 100% done, and log 2 hours of Development time with a note
> about the fix."*

Setting the status to **Resolved** moves the issue into the done category; logging time records it
against the issue with an activity (Development is the default).

## Append commit metadata after work

The cleanest way to tie code to an issue is through [metadata](../metadata/values.md), not free
text in the description. After committing, have the agent append the commit hash and branch to the
issue's array metadata fields (the **Software Development** schema provides `commits`, `branches`,
and `pull_requests`).

> *"On ACME-42, append commit hash `a1b2c3d` to the commits metadata and `fix/export-button` to
> branches."*

Because these are array fields, appending adds to the list without overwriting earlier entries, so
the metadata stays the single source of truth for what shipped. See
[Setting metadata values](../metadata/values.md).

## Build a wiki page incrementally

The agent can draft and grow a [wiki](../wiki/index.md) page over a conversation. Create it first,
then add sections as you refine the content.

> *"Create a wiki page titled 'Deployment Runbook' with an overview, then append a section on
> rollback steps once I confirm them."*

Use **spaces** in the title (the slug is derived automatically), and let the agent append or edit
specific sections rather than rewriting the whole page — every edit is versioned, so you can always
review history or revert.

!!! tip "Be specific about the project and issue"
    Agents key off `project_key` (like `ACME`) and `issue_ref` (like `ACME-42`). Naming them in your
    request removes ambiguity and saves the agent a lookup. When in doubt, ask it to run
    `specivo_whoami` and `specivo_list_lookups` first to confirm where it is and what it can use.
