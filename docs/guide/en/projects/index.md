---
description: What a project is in Specivo — its key, identifier, parent nesting, visibility, color, the member roles, project status, and the overview area.
---

# Projects

A **project** is the container that holds a body of work: its issues, its [wiki](../wiki/index.md),
its [versions](versions.md), its [sprints](sprints.md), and a member list with roles. Every issue,
wiki page, and time entry belongs to exactly one project. Most of what you do in Specivo happens
inside one.

![The projects list](../../assets/img/projects-list.webp)

## What defines a project

| Field | What it holds |
|---|---|
| **Name** | The human-readable title, shown in lists and headers |
| **Key** | A short **uppercase** prefix like `ACME` that brands every issue (`ACME-1`, `ACME-42`). An administrator can rename it; see [Renaming a project key](#renaming-a-project-key). |
| **Identifier** | The URL slug used in links, e.g. `/projects/acme/` |
| **Parent** | An optional parent project, so you can nest related projects under one umbrella |
| **Visibility** | **Public** (anyone who can reach the site can read it) or **private** (members only) |
| **Color** | A color used to tint the project in lists and badges so it's easy to spot |

The **key** is the part you'll type and paste most. Once people learn that `ACME-42` is an issue in
the ACME project, that reference works everywhere — in chat, in commit messages, and in
[search](../issues/finding.md).

### Renaming a project key

An administrator can rename a project's key, for example from `OLD` to `ACME`, through the REST API:
`POST /api/v1/admin/projects/OLD/rename/` with `{"new_key": "ACME"}`. Every issue in the project is
re-keyed, so `OLD-12` becomes `ACME-12`. Old references keep working and lead to the issue under its
new key: links to `/issue/OLD-12/`, `OLD-12` written in descriptions and comments, and `OLD-12` in
commit messages. The old key stays reserved for this project, so no other project can take it.

## Member roles

People join a project with a **role** that decides what they can do. Specivo ships with four:

| Role | For | Can do |
|---|---|---|
| **Manager** | Project leads | Full control — settings, members, versions, sprints, workflow, and all issue and wiki actions |
| **Developer** | People doing the work | Create and update issues, log time, edit wiki pages, comment |
| **Reporter** | Stakeholders and requesters | File and comment on issues, follow progress |
| **Agent** | Automation and API/[MCP](../ai-agents/index.md) accounts | A role scoped for service accounts that act through the API |

The **Agent** role exists for AI assistants and automation. Teams usually create a dedicated service
account, give it the Agent role on the projects it should touch, and issue it an API key. See
[AI agents & MCP](../ai-agents/index.md).

A fifth role, **Automation**, is not offered when adding members: it is the role the built-in
**Git** account gets by default (a manager may pick another) when a project turns on
[merge automation](../integrations/repositories.md#merge-automation) for a connected repository.

!!! note "Roles are per project"
    A person can be a Manager in one project and a Reporter in another. Membership and role are set on
    each project, not globally.

### What a role can see

Besides its permissions, each role has two visibility settings, edited by an administrator under
**Administration → Roles**:

| Setting | Values | Default |
|---|---|---|
| **Users visibility** | **All active users**, or **Members of visible projects** — the person themselves plus everyone who is a member (directly or through a group) of a project they can open | All active users |
| **Time entries visibility** | **All time entries** in the project, or **Own time entries** only | All time entries |

*Users visibility* limits the people a member can find in user pickers (for example when adding
a project member or typing a name) and in an issue's watcher list. *Time entries visibility*
limits the project's Time area, an issue's time tab and its logged total, a version's logged total,
billable-time reports and the dashboard's agent activity. Restricting time is the usual setup for
contractors on a billable project: they log and see their own hours and nobody else's.

Time entries visibility only matters for roles that have the **View time entries** permission;
a person whose roles in a project lack it sees no time there at all, not even their own.

When someone holds several roles, the **widest** setting wins, as in Redmine: one role with
*View time entries* and *All time entries* on a project is enough to see every entry there, whatever
their other roles say.
For users, any role on any of their projects with *All active users* is enough; a person without
any project role follows the built-in **Non member** role. Administrators always see everything.

## Project status

A project is in one of three states:

- **Active** — the normal state; the project is open for work.
- **Closed** — work has wound down. The project stays fully readable, but it's marked as no longer
  active so it drops out of the day-to-day view.
- **Archived** — kept for the record. The project is preserved but tucked away.

## The project overview

The **overview** is the project's home page. It pulls together the project's name, description, and
quick links to its issues, [roadmap](versions.md), [sprints](sprints.md), [wiki](../wiki/index.md),
and time, so you can see the shape of the project at a glance and jump to the area you need.

![A project overview](../../assets/img/project-overview.webp)

From here you typically branch out to:

- [Versions & roadmap](versions.md) — group issues toward releases and watch progress.
- [Sprints](sprints.md) — plan and run time-boxed iterations.
- [Recurring tasks](recurring-tasks.md) — patterns that generate issues automatically on a schedule.
- [Time tracking](time-tracking.md) — log hours and read reports.
