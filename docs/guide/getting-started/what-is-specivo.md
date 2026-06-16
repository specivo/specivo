---
description: What Specivo is, who it's for, and how its tracker, wiki, and AI integration fit together.
---

# What is Specivo?

Specivo is a **self-hosted** platform that combines three tools most teams otherwise stitch together:

1. A **project tracker** — issues, sprints, releases, and reporting.
2. A **wiki** — your team's living knowledge base, version-controlled.
3. An **AI integration layer** — a built-in MCP server so AI assistants can do real work in the tracker.

Because you run it yourself, your issues, documents, and search index never leave infrastructure you
control. There is no per-seat license; you add as many teammates as you need.

## Who it's for

Specivo is built for small-to-medium teams who want one place for *what we're doing* and *what we know*,
and who increasingly work alongside AI agents. If your decisions vanish into chat, your docs are scattered
across five apps, and your AI assistants ship work in a black box, Specivo is designed to fix that.

## How the pieces fit together

Everything in Specivo connects:

- An **issue** can link to related issues, carry [structured metadata](../metadata/index.md), hold file
  [attachments](../issues/attachments.md), and reference a [wiki](../wiki/index.md) page.
- A **wiki page** can cross-link to other pages and be referenced from issues.
- **Search** spans issues, wiki pages, comments, and attachment descriptions at once.
- **AI agents** reach all of the above through the [MCP server](../ai-agents/index.md), and every change
  they make is recorded just like a change made by a person.

!!! note "AI is optional"
    Specivo works fully without any AI features. Semantic search and the MCP server are there when you
    want them — using a bundled on-device model or your own API key — and out of the way when you don't.

## What this guide covers

This is the **end-user and operator guide**. It explains how to *use* Specivo (issues, metadata, wiki,
AI agents) and how to *install and run* it ([Installing Specivo](../install/index.md)). It is not an API
or source-code reference.

Ready? Continue to **[Core concepts](core-concepts.md)**, or jump straight into
**[Your first 10 minutes](first-10-minutes.md)**.
