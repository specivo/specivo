---
description: Specivo is a self-hosted project tracker, team wiki, and AI-safe automation platform. Track issues, link related work, search by meaning, let AI agents work inside the tracker, and see what your AI work costs per person and feature.
---

# Specivo user guide

**Specivo** is a self-hosted platform that keeps your team's work and knowledge in one place: a
project tracker, a wiki, and an integration layer that lets AI agents work *inside* the tracker
instead of around it. You run it on your own server, you own the data, and there is no per-seat pricing.

This guide is for the people who use Specivo every day — creating and tracking issues, writing wiki
pages, and connecting AI assistants. If you want to install and operate Specivo, jump to
[Installing Specivo](install/index.md). Moving from another tracker, such as Jira? See [Importing from another tracker](install/importing.md).

![The Specivo dashboard](../assets/img/hero-dashboard.webp)

## Four things Specivo does

- **Track work.** Issues with types, statuses, priorities, assignees, subtasks, and
  [relations](issues/relations.md) between them. Group work into
  [projects, versions, and sprints](projects/index.md).
- **Keep knowledge.** A per-project [wiki](wiki/index.md) with Markdown, full version history, and
  one-click revert. Cross-link pages and issues so decisions never get lost.
- **Work with AI agents.** A built-in [MCP server](ai-agents/index.md) lets AI assistants read and
  update issues, wiki pages, and search — safely, with everything they do over MCP recorded in the
  [audit log](install/audit-log.md). People's own edits are kept in issue and wiki history.
- **Track AI spending.** See what each person, project and feature costs in AI, across Claude Code,
  Codex, Gemini CLI, OpenCode and other tools, on personal plans, team seats and API keys. Usage is
  [reported automatically](ai-agents/reporting-ai-usage.md), tied to the issue it was spent on, and
  rolled up in [AI spend reports](projects/ai-spend-reports.md).

## New here?

If this is your first time in Specivo, start with **[Your first 10 minutes](getting-started/first-10-minutes.md)** —
it walks you through signing in, finding your project, opening an issue, and leaving a comment. Then read
**[Core concepts](getting-started/core-concepts.md)** to learn the handful of words (issue, tracker, version,
sprint, wiki) that the rest of this guide uses.

## Find your way around

| If you want to… | Read |
|---|---|
| Understand what an issue is and create one | [Issues](issues/index.md) → [Creating an issue](issues/creating.md) |
| Link issues that block, duplicate, or relate to each other | [Relations](issues/relations.md) |
| Add structured fields like story points or git branches | [Issue metadata](metadata/index.md) |
| Plan releases and sprints | [Versions & roadmap](projects/versions.md), [Sprints](projects/sprints.md) |
| Schedule issues to recur automatically | [Recurring tasks](projects/recurring-tasks.md) |
| Write and organize documentation | [Wiki & knowledge base](wiki/index.md) |
| Link commits and pull requests from GitHub, GitLab, Gitea or Forgejo to issues | [Repository integrations](integrations/repositories.md) |
| Notify another system when issues or wiki pages change | [Outgoing webhooks](integrations/webhooks.md) |
| Connect Claude Code, Codex, or another AI client | [AI agents & MCP](ai-agents/index.md) |
| See what your team spends on AI, per person, project and feature | [AI usage](projects/ai-usage.md), [AI spend reports](projects/ai-spend-reports.md) |
| Install or self-host Specivo | [Installing Specivo](install/index.md) |
| Let people sign in with your identity provider | [Single sign-on](install/single-sign-on/index.md) |
| Move over from another tracker, such as Jira | [Importing from another tracker](install/importing.md) |

!!! tip "Search is built in"
    Press <kbd>⌘K</kbd> (<kbd>Ctrl+K</kbd>) anywhere in Specivo to search. It uses **hybrid search** — exact keywords *and*
    meaning — so it finds work even when you wrote "login flow" once and "auth sequence" later. See
    [Finding issues](issues/finding.md).
