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

Besides its permissions, each role has three visibility settings, edited by an administrator under
**Administration → Roles**:

| Setting | Values | Default |
|---|---|---|
| **Users visibility** | **All active users**, or **Members of visible projects** — the person themselves plus everyone who is a member (directly or through a group) of a project they can open | All active users |
| **Time entries visibility** | **All time entries** in the project, or **Own time entries** only | All time entries |
| **AI usage visibility** | **All AI usage** in the project, or **Own AI usage** only | Own AI usage (the built-in Manager role: All AI usage) |

*Users visibility* limits the people a member can find in user pickers (for example when adding
a project member or typing a name) and in an issue's watcher list. *Time entries visibility*
limits the project's Time area, an issue's time tab and its logged total, a version's logged total,
billable-time reports and the dashboard's agent activity. Restricting time is the usual setup for
contractors on a billable project: they log and see their own hours and nobody else's.

Time entries visibility only matters for roles that have the **View time entries** permission;
a person whose roles in a project lack it sees no time there at all, not even their own.

*AI usage visibility* works the same way for AI usage entries (the tokens and spend of people's
AI coding tools) and only matters for roles with **View AI usage**, with one difference: everyone
always sees the AI usage logged in their own name, and which account logged it, whatever their
roles. A role set to **All AI usage** shows each person's usage and spend in the project, so only
give it to the people who should see that, such as project leads; the default shows people their
own usage only. Entries on issues someone cannot see stay hidden from them unless the entries are
their own.

The AI usage permissions are **View AI usage**, **Log AI usage** (log your own usage in the
project), **Edit/exclude any AI usage entries**, and **Log AI usage on behalf of other members**,
which a shared service account needs to log usage for the people it reports for; those people must
be active members of the project. Usage that belongs to no project can be logged by anyone for
themselves, and by administrators for others. Moving someone else's entry to another project needs
the same on-behalf permission in that project. The built-in roles get: Manager all four, Developer
and Agent view and log, Reporter view.

When someone holds several roles, the **widest** setting wins, as in Redmine: one role with
*View time entries* and *All time entries* on a project is enough to see every entry there, whatever
their other roles say.
For users, any role on any of their projects with *All active users* is enough; a person without
any project role follows the built-in **Non member** role. Administrators always see everything.

### Similar issue suggestions

The **General** tab of a project's settings has a **Suggest similar issues on the new issue form**
box, ticked by default. While someone types the subject of a new issue, the form lists issues of
the project with a similar subject (see [Similar issues](../issues/creating.md#similar-issues)).
Untick it to turn the list off for this project. When an administrator has turned the suggestions
off for the whole instance, the box has no effect and says so.

### Reporting currency

Money reports for a project, such as AI spend, are shown in the instance's reporting currency. To
use another one for this project, open the **General** tab of its settings, pick a currency under
**Reporting currency** and click **Save**. **Instance default** follows the instance setting again.
Only currencies an administrator has enabled are offered; see
[Currencies and the reporting currency](../install/currencies.md). Changing it needs the *Manage
project* permission and is recorded in the audit log.

### Public projects and search engines

A public project that an administrator opts in to anonymous access can be read without an account
while the instance's anonymous access switch is on. Visitors can also search it, with the same
hybrid search as everyone else, within a per-address limit (see
[Search rate limits](../install/configuration.md#search-rate-limits)). Its project page, issue list,
issues and wiki pages can also appear in search engines, but only when an administrator turns on
**Let search engines index public pages** under **Administration → Settings → Anonymous access**
(off by default). Private issues and comments, and every page of a project that is not opted in,
are never indexed. See [Search engines](../install/configuration.md#search-engines) for what is
listed in `robots.txt` and `/sitemap.xml`, and how to keep one public project out.

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
