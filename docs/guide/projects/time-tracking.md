---
description: Time tracking in Specivo — log hours against an issue or project with an activity, track estimate versus spent, and read the project Time area.
---

# Time tracking

Specivo lets you **log time** against the work you do — hours spent on an issue or a project, tagged
with the kind of activity. Logged time rolls up into reports so you can see where effort goes and
compare it against your estimates.

## Who can do what

Three role permissions control time tracking in a project:

| Permission | Allows |
|---|---|
| **View time entries** | Reading time: the project's Time area, an issue's time tab and logged total, a version's logged total and billable-time reports. Without it you see no entries, not even your own. |
| **Log time** | Logging time, by hand or with a timer, and editing or deleting your own entries (as long as you can see them). |
| **Edit/delete any time entries** | Editing or deleting anyone's entries in the project. |

Out of the box, **Manager** has all three, **Developer** and **Agent** can view and log time, and
**Reporter** and the built-in **Non member** role have none. Without *Edit/delete any time entries*
you can still edit or delete your own entries, as long as you have *Log time* in the project. Administrators
can do everything. Buttons you can't use (*Log Time*, the log-time form on an issue, edit controls)
are hidden.

A running timer is checked again when you stop it: if you have lost the right to log time on its
project or issue in the meantime, the timer is discarded and no time is written.

## Log time against an issue

Open the issue and add a time entry: the number of **hours**, the **date**, an **activity**, and an
optional comment. Logging against an issue is the common case, because it ties the effort to a
specific piece of work. You can also log time at the project level for work that doesn't map to one
issue.

### Activities

Every entry is tagged with an **activity** so reports can break effort down by type:

| Activity | For |
|---|---|
| **Development** | Writing and changing code — the default |
| **Design** | UI, UX, and design work |
| **Testing** | QA, test writing, verification |
| **Meetings** | Planning, standups, reviews |
| **Support** | Helping users, answering requests |

**Development** is selected by default, so the common case is one click less.

## Estimate versus spent

Issues carry an **estimate** — estimated hours, with original and remaining estimates — alongside the
time actually **logged**. Comparing the two tells you whether a piece of work is on track: an issue
estimated at 8 hours with 12 logged and still open is a signal worth a look. See the full field set
in [What is an issue?](../issues/index.md).

## The project Time area

Each project has a **Time** area that lists time entries and rolls them into reports — totals by
activity, by person, and over a date range — so a [Manager](index.md) can see how effort is spread
across the project.

!!! note "Who sees whose time"
    By default everyone with **View time entries** sees every entry. A role set to **Own time entries**
    (see [What a role can see](index.md#what-a-role-can-see)) sees only the hours its holder logged —
    in the Time area, on issues, in version totals and in reports. A person with several roles gets
    the widest setting among them.

![The project time entries](../assets/img/time-entries.webp)
