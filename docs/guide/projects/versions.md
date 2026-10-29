---
description: Versions in Specivo — milestones and releases with a status and target date, assigning issues to a version, and reading the roadmap.
---

# Versions & roadmap

A **version** is a milestone or release: a named target that issues are grouped toward, such as
`v1.2` or "Q3 launch". Versions are how you plan ahead — you assign issues to a version, then watch
the **roadmap** fill in as the work gets done.

## What a version holds

| Field | What it holds |
|---|---|
| **Name** | The release or milestone name, e.g. `v1.2` |
| **Description** | A short summary of what the version is for |
| **Status** | **Open** (accepting work), **locked** (no new issues, work in progress finishes), or **closed** (shipped/done) |
| **Target date** | The effective date you're aiming to deliver by |
| **Sharing** | The scope the version is shared with (for example, sub-projects) |
| **Wiki page** | An optional linked [wiki page](../wiki/index.md) — handy for release notes |

Sharing decides which projects a version is offered in. It never shows a version to someone who
can't open the project that owns it: a shared version of a private project you don't belong to stays
hidden from you.

The optional **wiki page** link is a good place to keep release notes: write the page once, link it
from the version, and everyone reaching the roadmap can find the notes.

## Assign an issue to a version

Open the issue and set its **target version** field to the version you want. That's the single link
between an issue and a release — once set, the issue counts toward that version's progress and shows
up under it on the roadmap. See [Updating an issue](../issues/updating.md).

Only the three roadmap trackers — **Bug**, **Feature**, and **Task** — appear on the roadmap.
**Support** issues don't, so everyday requests don't crowd your release planning.

## The roadmap

The **Roadmap** lists the project's versions and shows progress for each: how many of its issues are
done versus still open. It's the at-a-glance answer to "what's left before we ship?".

![The project roadmap](../assets/img/roadmap.webp)

### Order and completed versions

By default the roadmap lists the versions still in play — **open** and **locked** — soonest target
date first; versions without a target date come after the dated ones. **Closed** versions are
hidden: click **Show completed versions** to list them in their own section below the active
ones, and **Hide completed versions** to tuck them away again.

Use the sort menu at the top of the roadmap to order versions by:

- **Due date** — soonest or latest first (undated versions always last)
- **Version number** — lowest or highest first
- **Status** — open first, or closed first

Version numbers sort the way people read them, not letter by letter: `0.2.0` comes before
`0.10.0`, and `Sprint 9` before `Sprint 10`. Names are still free text, so `Q3 launch` or
`v1.2-rc1` work too.

Specivo remembers your choice. The same sort applies to the versions list in the project's
settings, and changing it in either place changes both. It is a personal setting — other people
keep their own order.

Progress on the roadmap, on a version's page and in the project settings counts only the issues you are
allowed to see, so a private issue never shows up in a number you could not click through to.

Click a version to see the issues assigned to it, grouped by status, so you can drill from the
high-level plan straight into the work.

## Creating and managing versions

Versions are created and edited by a project **Manager** in the project's settings. Set the name and
target date, leave the status **open** while you're still adding issues, move it to **locked** as you
near the finish, and **close** it once the release ships.

!!! warning "You can't delete a version that's still in use"
    Deleting a version **fails while issues still reference it**. Specivo won't orphan those issues.
    First reassign or clear the **target version** on every issue pointing at it, then delete the
    version.
