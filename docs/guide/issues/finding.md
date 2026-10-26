---
description: Finding issues in Specivo — the filterable issue list, the board view, and hybrid keyword-plus-semantic search across issues, wiki, comments, and attachment descriptions.
---

# Finding issues

There are three ways to get to the issue you want: the **list**, the **board**, and **search**. Pick
whichever matches what you're doing.

![The issues list](../assets/img/issues-list.webp)

## The issue list

The **Issues** list shows every issue in the project as rows, with columns for the key (like `ACME-42`),
subject, tracker, status, priority, and assignee. **Filter** the list to narrow it down — by status,
tracker, assignee, version, sprint, and more — and click any row to open the issue.

!!! tip "Filter by metadata or tags"
    If your project defines [metadata](../metadata/index.md), you can filter the list by a metadata
    `key=value` match. For array fields, the filter matches when the array *contains* the value — for
    example, every issue whose `branches` includes `release/1.2`. You can also filter by
    [tags](../projects/tags.md) from the search page — clicking any tag chip jumps straight there.

## The board

The **board** view lays issues out as cards in columns by status category (backlog, active, done,
closed). It's the quickest way to see *where everything stands* at a glance, and issues move across the
columns as their [status](statuses-workflow.md) changes.

## Search

Press <kbd>⌘K</kbd> (<kbd>Ctrl+K</kbd> on Windows/Linux) anywhere in Specivo to jump to search and start typing.

![Search](../assets/img/search.webp)

Specivo uses **hybrid search** — it combines exact **keyword** matching with **semantic** (meaning-based)
matching, so it finds the right work even when the wording differs. (The semantic half needs an
embedding model that your administrator has installed; see below.) Search "auth flow" and it can still
surface an issue you wrote about the "login sequence". It searches across:

- issues (subjects and descriptions)
- wiki pages
- comments
- attachment descriptions

!!! tip "Search by issue key"
    Type an issue key such as `ACME-42` (in any case) to put that issue at the top of the results, in
    any search mode. A key the issue had before it was [moved](updating.md#move-an-issue-to-another-project) to another project, or
    under a project's old key after a rename, finds it too. Below it you get the issues, comments,
    and wiki pages that mention the key. Keys from other trackers that appear in your text, like
    `DO-8795`, are found the same way. You can add other words too: `ACME-42 login` finds what
    mentions both.

!!! note "Semantic results not showing up?"
    Semantic search relies on an embedding model and an index that the operator sets up on the server.
    Until that's in place, keyword search still works — you just won't get the meaning-based matches.
    New comments, and issues and wiki pages edited after the last indexing run, may be missing or
    out of date in search until the operator re-runs it. Operators: see
    [Semantic search and embeddings](../install/semantic-search.md). See
    [Troubleshooting](../reference/troubleshooting.md) if results look thin.
