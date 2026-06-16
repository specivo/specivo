---
description: One-line definitions of every Specivo term — from Agent role and API key through tracker, version, watcher, and % done.
---

# Glossary

Every term Specivo uses, defined in one line. Heavier terms link to their full page.

| Term | Definition |
|---|---|
| **% done** | An issue's completion percentage (0–100). Reaching a *done* status such as **Resolved** sets it to 100. See [Statuses & workflow](../issues/statuses-workflow.md). |
| **Agent** (role) | The member role intended for API and automation accounts that act through the [MCP server](../ai-agents/index.md). |
| **API key** | A secret string starting `spv_…` that authenticates REST and MCP requests on behalf of a user. See [API keys](../ai-agents/api-keys.md). |
| **attachment** | A file uploaded to an issue, wiki page, or comment, with its own filename, size, type, and searchable description. See [Attachments](../issues/attachments.md). |
| **backlog** | The pool of unscheduled issues a team draws from when planning a sprint. See [Sprints](../projects/sprints.md). |
| **board** | A column view of issues grouped by status category (backlog / active / done / closed). Issues and sprints both have a board. |
| **category** | A project-scoped label for grouping issues, such as "Backend" or "Frontend". Not to be confused with a status category. |
| **Developer** (role) | The member role for people doing the work — they create and update issues but can't manage project settings. |
| **issue** | The unit of work: a bug, feature, task, or support request, numbered per project. See [Issues](../issues/index.md). |
| **issue_ref** | An issue's project-prefixed identifier, like `ACME-12`. Used in links and by AI agents to address a specific issue. |
| **Manager** (role) | The member role with full control of a project, including settings, members, and metadata schemas. |
| **MCP server** | Specivo's built-in Model Context Protocol server, which lets AI clients read and write Specivo. See [AI agents](../ai-agents/index.md). |
| **metadata** | Typed key/value fields on an issue beyond its core fields, defined by a metadata schema. See [Issue metadata](../metadata/index.md). |
| **metadata schema** | A JSON-Schema-based definition of which metadata fields apply to a project or tracker. See [Metadata schemas](../metadata/schemas.md). |
| **priority** | How urgent an issue is: Low, Normal (default), High, Urgent, or Immediate. |
| **project** | A container for a body of work — issues, wiki, versions, sprints, and members. See [Projects](../projects/index.md). |
| **project key** | The short uppercase prefix that brands every issue in a project, like `ACME` (giving `ACME-1`). |
| **relation** | A typed link between two issues — relates, blocks, duplicates, precedes, or copied. See [Relations](../issues/relations.md). |
| **Reporter** (role) | The member role for people who file and comment on issues but don't take ownership of the work. |
| **roadmap** | The project view that shows versions and their progress. See [Versions & roadmap](../projects/versions.md). |
| **slug** | The normalized URL form of a wiki page title; `My Page`, `my-page`, and `My_Page` all resolve to the same slug. See [Linking & history](../wiki/history-linking.md). |
| **sprint** | A time-boxed iteration with a goal, a start and end date, and its own board. See [Sprints](../projects/sprints.md). |
| **status** | Where an issue sits in its lifecycle — New, In Progress, Resolved, Feedback, Closed, Rejected. See [Statuses & workflow](../issues/statuses-workflow.md). |
| **subtask** | An issue nested under a parent issue (up to 16 levels deep). See [Subtasks](../issues/subtasks.md). |
| **tracker** | The *type* of an issue: Bug, Feature, Task, or Support. See [Issues](../issues/index.md). |
| **version** | A milestone or release that groups issues toward a target date. See [Versions & roadmap](../projects/versions.md). |
| **watcher** | A user who follows an issue to receive notifications about changes to it. |
| **wiki** | A project's Markdown knowledge base, with version history, nesting, and cross-links. See [Wiki](../wiki/index.md). |
