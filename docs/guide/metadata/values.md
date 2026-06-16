---
description: Working with metadata values on a Specivo issue — viewing fields, setting string, enum, and date values, editing array fields, and filtering issue lists by metadata.
---

# Working with metadata values

Once a [schema](schemas.md) applies to an issue, its [metadata](index.md) fields are yours to fill in. This
page covers viewing the values, setting them, editing list (array) fields, and filtering issue lists by
metadata.

## Viewing metadata

Open any issue. Metadata fields appear in their own section on the issue detail page, alongside the core
fields. Fields that haven't been filled in yet show as empty.

## Setting a value

Edit the issue and set each field according to its type:

- **String** — type the text (for example `component` = `Backend`).
- **Number / integer** — enter a numeric value (for example `story_points` = `5`).
- **Date** — pick a date from the date picker.
- **Enum** — choose one option from the field's fixed list (for example `severity` = `major`).

Specivo validates as you go: an enum only accepts its allowed choices, and a number field rejects non-numeric
input. Save the issue to store the values.

## Editing array fields

An **array** field holds a list of values — multiple branches, several tags, a set of pull requests. Add
items one at a time, and remove any item you no longer need. For example, a `branches` field might start with
`main` and grow to `main`, `feature/login` as work spreads across branches. There's no limit to mixing this
with the other fields on the issue.

## Filtering issue lists by metadata

Metadata pays off when you search across issues. Issue lists can **filter by metadata** using a key=value
match:

- Pick the metadata key, then the value to match — for example `severity = critical` to see only critical
  bugs, or `content_status = draft` to find everything still in draft.
- For **array** fields, the match succeeds if the array **contains** the value. Filtering `branches` by
  `feature/login` returns every issue whose branch list includes `feature/login`, even when it lists other
  branches too.

This is the payoff over free text: because the values are typed and consistent, the filter returns exactly
the issues you mean.

!!! tip "AI agents can set metadata too"
    Connected AI agents can read and update metadata through the built-in MCP server — for example appending
    a commit hash to a `commits` array or tagging an issue as it works. See
    [AI agent workflows](../ai-agents/workflows.md).
