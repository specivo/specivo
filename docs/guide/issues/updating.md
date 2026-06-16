---
description: How to edit an issue in Specivo — change status, reassign, edit the subject or description, set % done, and update fields. Every change is recorded in history.
---

# Updating & editing issues

Issues change as work moves forward. You can edit almost any field on an issue at any time, and
Specivo keeps a record of what changed.

![Editing an issue](../assets/img/issue-edit.webp)

## Change the status

The most common update is moving an issue along its lifecycle. Set the status to **In Progress** when
you start, and to **Resolved** when you finish. Resolving an issue sets **% done** to 100
automatically. See [Statuses & workflow](statuses-workflow.md) for the full set of statuses and how
they drive the board.

## Reassign

Change the **Assignee** to hand the issue to someone else. The new assignee is responsible for it from
then on; add **watchers** if other people still need to follow along.

## Edit the subject and description

Click into the **subject** or **description** to fix wording, add detail, or restructure. The
description is [Markdown](../reference/markdown.md), so formatting, code blocks, and `@username`
mentions all work.

## Set % done and other fields

- Nudge **% done** as work progresses (0–100). It moves to 100 when the issue is resolved or closed.
- Adjust **priority**, **category**, **target version**, **sprint**, **start/due dates**, and the
  **estimate** as plans firm up.
- Fill in any [metadata](../metadata/index.md) fields the project defines.

!!! note "Comments vs field edits"
    To discuss the work or post an update, add a [comment](comments.md). To record progress on the
    work itself, change the fields. Both end up in the issue's history.

## Everything is recorded

Editing the core fields of an issue — status, assignee, priority, dates, % done, and so on — is logged
in the issue's **history**, with who changed what and when. Nothing is silently overwritten, so you can
always see how an issue got to where it is.
