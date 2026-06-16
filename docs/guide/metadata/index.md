---
description: What metadata is in Specivo — typed key/value fields beyond the core issue fields, why it beats free text, the field types, and how it appears on an issue.
---

# What is metadata?

**Metadata** is structured, typed data attached to an issue beyond its core fields. Where the core fields
cover the basics every issue needs — subject, status, assignee, priority — metadata captures the extra facts
your team cares about: a git branch, a story-point estimate, a severity level, a publish date. Each piece of
metadata is a named **key** with a typed **value**, defined by a [schema](schemas.md) so the same fields stay
consistent across every issue that uses them.

## Why not just write it in the description?

You *could* type "branch: feature/login, 5 points, severity major" into the description. Metadata is better
because it is **typed and structured**, not free text:

- **Consistent.** Everyone fills the same fields the same way. No "5 pts" on one issue and "five story
  points" on the next.
- **Filterable.** Issue lists can [filter by metadata](values.md), so you can pull up every issue with
  `severity = critical` or every issue touching a given branch. Free text in a description can't do that
  reliably.
- **Validated.** An `enum` field only accepts its allowed choices; a `number` field only accepts numbers.
  Bad values are caught as you type instead of discovered later.

## Field types

A schema defines each field with a type, which controls what you can enter and how Specivo validates it.

| Type | What it holds | Example |
|---|---|---|
| **String** | Free text on a single line | `component` = `Backend` |
| **Number / integer** | A numeric value | `story_points` = `5` |
| **Date** | A calendar date | `publish_date` = `2026-07-01` |
| **Enum** | One choice from a fixed list | `severity` = `critical` |
| **Array** | A list of values (add or remove items) | `branches` = `[main, feature/login]` |

Array fields are how you record several values under one key — multiple git branches, a set of tags, a list
of pull requests — without cramming them into a single string.

## How metadata appears on an issue

When a schema applies to an issue, its fields show up in their own section on the issue detail page,
alongside the core fields. You fill them in like any other field: type a string, pick a date, choose an enum
value, or add items to an array. Fields you leave blank simply stay empty.

![Metadata fields on an issue](../assets/img/metadata-issue.webp)

## Next steps

- [Metadata schemas](schemas.md) — how fields are grouped and attached to projects and trackers, plus the
  built-in presets.
- [Working with metadata values](values.md) — setting values, editing array fields, and filtering issue
  lists by metadata.
