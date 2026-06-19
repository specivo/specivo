---
description: Tags in Specivo — per-project labels that you can attach to issues and wiki pages, managed in project settings, and filterable across all your projects from the search page.
---

# Tags

**Tags** are short, colored labels you attach to issues and wiki pages to categorize your work beyond
the built-in tracker, status, and priority fields. A tag is defined once inside a project and can then
be applied to any number of issues or wiki pages within that project.

<!-- screenshot: tags in project settings -->

## Tags vs metadata

Tags and [metadata](../metadata/index.md) are separate features and serve different purposes:

| | Tags | Metadata |
|---|---|---|
| **Shape** | Plain named labels (optionally colored) | Structured key-value fields with schemas |
| **Best for** | Quick categorical labeling (e.g. "backend", "needs-review") | Typed data fields (e.g. branch name, environment, story points) |
| **Applied to** | Issues and wiki pages | Issues |
| **Managed by** | Project Managers (vocabulary); any member (applying) | Schema owners |
| **Filterable** | Yes — cross-project tag filter on the search page | Yes — metadata containment filter |

If you want a simple label, use a tag. If you want a structured field with a type and a schema, use
metadata.

## Managing the tag vocabulary

Tags are created, renamed, recolored, and deleted in **Project Settings** under the **Tags** tab.
This area also shows how many issues and wiki pages each tag is currently used on, so you can safely
prune unused labels.

<!-- screenshot: tag vocabulary in project settings tabs -->

!!! note "Managers only"
    Only project **Managers** (and administrators) can create, rename, recolor, or delete tags. Any
    project member can apply existing tags — or create new ones on the fly — when editing an issue or
    wiki page.

### Creating a tag

1. Open the project's **Settings** and click the **Tags** tab.
2. Click **New tag**.
3. Type the **name** (up to 64 characters).
4. Optionally pick a **color** using the hex color field (e.g. `#4f9d6c`). The color is used for the
   colored swatch on the tag chip wherever the tag appears.
5. Click **Create tag**.

Tag names are **case-insensitively unique** within a project: you cannot have both `Backend` and
`backend` — they are treated as the same name.

### Renaming and recoloring a tag

Click the edit icon next to any tag in the Tags tab. Change the name, the color, or both, then
**Save changes**. All places the tag is displayed update immediately — there is no need to reattach
it.

### Deleting a tag

Click the delete icon for the tag and confirm. Deleting a tag removes it from **all** issues and wiki
pages it was applied to. This cannot be undone.

!!! warning "Deletion removes the tag everywhere"
    If a tag is applied to 40 issues and 10 wiki pages, deleting it detaches it from all 50 at once.
    Check the usage counts in the Tags tab before deleting.

## Tagging issues

Tags appear on the issue sidebar. You can add and remove them when **viewing** or **editing** an
issue.

<!-- screenshot: tag chips on an issue sidebar -->

To apply a tag, type in the **Add tags** field on the issue sidebar. As you type, an autocomplete
list appears showing matching tags from the project. Select one to apply it. If you type a name that
does not exist yet, it is **created on the fly** in the project's vocabulary, so you don't need to
pre-populate the settings first.

To remove a tag from an issue, click the remove control on its chip.

!!! tip "Creating tags on the fly"
    Any member can type a new name in the tag input and the tag is created automatically. The new tag
    immediately appears in Project Settings → Tags so it can be renamed, recolored, or removed by a
    Manager later.

## Tagging wiki pages

Tags work the same way on wiki pages. When you view or edit a wiki page, a **tags field** appears
below the page content. Add and remove tags there just as you would on an issue.

<!-- screenshot: tag field on a wiki page -->

Tags attached to a wiki page show up as clickable chips on the page itself, alongside the author and
last-edited information.

## Clicking a tag chip

Every tag chip is a **link**. Clicking it opens the [search page](../issues/finding.md#search)
pre-filtered to show all issues and wiki pages that carry that tag across every project you can
access. This is the fastest way to jump from one item to everything else tagged the same way.

The link target is `/search/?scope=all&tag=<tag-name>`. You can bookmark these URLs.

## Filtering by tags in search

The search page has a dedicated **Filter by tags** control, separate from the text search field.

<!-- screenshot: tag filter on the search page -->

You can filter by multiple tags at once. When you apply more than one tag, the results use **AND
logic**: only items that carry **all** selected tags are returned.

Key behaviors:

- **Cross-project** — the filter spans every project you can access (member or public), not just the
  current one. Tags in private projects you are not a member of are excluded.
- **Case-insensitive matching** — typing `backend` matches a tag named `Backend`.
- **Autocomplete** — the input suggests tag names from your accessible projects as you type, with
  their colors where set.
- **Works with text queries** — you can combine a text search term with a tag filter; results must
  match both.
- **Works without a text query** — filtering by tags alone (no text query) lists all matching items
  ordered by most recently updated.
- **Covers issues and wiki pages** — the tag filter always searches both, regardless of the scope
  tab active (switching the scope tab to "wiki only" shows only wiki results that match, and so on).

### Removing a tag filter

Click the remove control on a tag chip in the filter bar to deselect that tag. The page reloads with
the remaining filters applied.

## AI agents and tags

If you use Specivo with an AI agent over MCP, the agent has the same tagging capability. It can:

- List the project's tag vocabulary (with usage counts).
- Apply, remove, or replace the full tag set on an issue or wiki page.
- Create tags on the fly when applying (subject to the member's project role).
- Curate tags (create, rename, recolor, delete) if the account has the `manage_project` permission.

The MCP `specivo_tag` tool accepts an issue ref (e.g. `ACME-42`) or a wiki ref
(`wiki:ACME/some-slug`), an operation (`get`, `add`, `remove`, `set`), and a tag name or list of
names. See [What agents can do](../ai-agents/capabilities.md) for the full tool catalog.

## What's next

- [Finding issues](../issues/finding.md) — search and filter across the whole project.
- [Issue metadata](../metadata/index.md) — structured key-value fields for typed data.
- [Creating & editing wiki pages](../wiki/editing.md) — write and update your team knowledge base.
