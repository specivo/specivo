---
description: The core Specivo vocabulary — projects, issues, trackers, statuses, versions, sprints, wiki, and metadata — explained in a few lines each.
---

# Core concepts

Specivo uses a small, consistent vocabulary. Learn these eight words and the rest of the guide reads easily.

## Project

A **project** is a container for a body of work. It has a **key** — a short uppercase prefix like `ACME`
that brands every issue in it (e.g. `ACME-42`). Projects hold issues, a wiki, versions, sprints, and a
member list with roles. Projects can be nested. See [Projects](../projects/index.md).

## Issue

An **issue** is the unit of work — a bug, a feature, a task, or a support request. It has a subject, a
description, and fields like assignee, priority, and due date. Issues are numbered per project
(`ACME-1`, `ACME-2`, …). See [Issues](../issues/index.md).

## Tracker

A **tracker** is the *type* of an issue. Specivo ships with four: **Bug**, **Feature**, **Task**, and
**Support**. The tracker affects which fields and workflow apply. See
[What is an issue?](../issues/index.md).

## Status

A **status** is where an issue sits in its lifecycle. The defaults are **New → In Progress → Resolved**,
plus **Feedback**, **Closed**, and **Rejected**. Each status belongs to a category (backlog, active, done,
closed) that drives boards and reports. See [Statuses & workflow](../issues/statuses-workflow.md).

## Relation

A **relation** is a typed link between two issues — *relates to*, *blocks*, *duplicates*, *precedes*, and
so on. Relations capture dependencies and history. See [Relations](../issues/relations.md).

## Version

A **version** (also called a milestone or release) groups issues toward a target — for example `v1.2`.
Versions drive the **roadmap**. See [Versions & roadmap](../projects/versions.md).

## Sprint

A **sprint** is a time-boxed iteration with a backlog and a goal. You plan it, start it, and complete it;
Specivo snapshots the team's velocity when you do. See [Sprints](../projects/sprints.md).

## Wiki

The **wiki** is your project's knowledge base — Markdown pages with full version history, hierarchy, and
cross-links. See [Wiki & knowledge base](../wiki/index.md).

## Metadata

**Metadata** is structured, typed data attached to an issue beyond its core fields — story points, a git
branch, a severity level. Metadata is defined by **schemas** so the same fields stay consistent across
issues. See [Issue metadata](../metadata/index.md).

---

!!! tip "One word per concept"
    Specivo always says **issue** (never "ticket" or "task-item"), **wiki page**, **version**, and
    **sprint**. If you're searching the docs or the app, those are the words to use. The full list lives in
    the [Glossary](../reference/glossary.md).
