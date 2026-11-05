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

Each field is labelled with its **name**. A field defined with a title in its schema shows that title, in
any language, on the issue page, in the issue's history, in search filters, in the REST API and over MCP.
A field without a title is labelled with its key made readable (`story_points` reads "Story Points"). Fields
brought in by an [import](../install/importing.md) keep the name they had in the old system.

A field that holds a **person** or a **version** shows that person's or version's name rather than an
internal id, as long as you are allowed to see them; otherwise it shows the stored value.

## Setting a value

Edit the issue and set each field according to its type:

- **String** — type the text (for example `component` = `Backend`).
- **Number / integer** — enter a numeric value (for example `story_points` = `5`).
- **Date** — pick a date from the date picker.
- **Enum** — choose one option from the field's fixed list (for example `severity` = `major`).

Specivo validates as you go: an enum only accepts its allowed choices, and a number field rejects non-numeric
input. Save the issue to store the values.

## What is refused

When you save, Specivo checks the values against the issue's [schemas](schemas.md) and refuses a value
that does not match: a word in a number field, a choice that is not in the list, an item of a list that
breaks the field's pattern, a key the schema does not allow. The same check applies to the issue page,
the REST API (`422` with the code `validation_error` and the field as `metadata.<key>`) and
[AI agents](../ai-agents/index.md), whose metadata tool answers with an error naming the field. In the
metadata panel the message appears next to **Save Metadata**, and your edits stay so you can correct
them.

A new issue must also fill every field its schema marks as **required**.

**Specivo judges the change, not the whole issue.** Only the fields a save changes are checked:

- An issue that already holds a value that no longer matches (from before a schema was tightened, or
  from an import) stays editable. You can change its status, its other fields and its other metadata.
- Changing, adding to or removing a field that holds such a value is refused until the field matches.
  For a list, that includes appending a good item while an old bad one is still there: fix the list as
  a whole.
- Removing a required field is refused; leaving an old issue without it is not.
- Moving an issue to another project or tracker does not re-check values that stay the same.

Values of a [recurring task](../projects/recurring-tasks.md) are checked when the pattern is saved, so its
issues keep being created if a schema changes later.

To find the issues that hold values not matching their schemas, an administrator can run
`python -m specivo.cli.admin metadata-check` on the server (add `--project KEY` for one project). It
lists the issues and the field names per schema, never the values, and changes nothing. See
[Upgrading](../install/upgrading.md#metadata-values-are-validated).

## Editing array fields

An **array** field holds a list of values — multiple branches, several tags, a set of pull requests. Add
items one at a time, and remove any item you no longer need. For example, a `branches` field might start with
`main` and grow to `main`, `feature/login` as work spreads across branches. There's no limit to mixing this
with the other fields on the issue.

!!! note "Computed fields fill themselves in"
    A [project-derived (computed) field](schemas.md#project-derived-computed-fields) shows its value on
    every issue automatically and is **read-only** — there's nothing to set, and you can't override it.
    Its value comes from the project, so it updates on its own if the issue is
    [moved to another project](../issues/updating.md#move-an-issue-to-another-project).

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

You can also reach a metadata-filtered search straight from any issue. When an issue has **array**
metadata values (for example a `branches` or `tags` field), each value shows as a **clickable tag** in
the issue's metadata. Clicking a tag opens a search filtered to that same value across **every project you
can access** — a fast way to find everything tagged with a given branch, label, or category without
starting from a project list.

!!! tip "AI agents can set metadata too"
    Connected AI agents can read and update metadata through the built-in MCP server — for example appending
    a commit hash to a `commits` array or tagging an issue as it works. See
    [AI agent workflows](../ai-agents/workflows.md).
