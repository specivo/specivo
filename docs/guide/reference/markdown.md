---
description: The Markdown Specivo renders in issue descriptions, comments, and wiki pages — formatting, code blocks, @mentions, and [[wiki links]].
---

# Text formatting

Specivo renders **Markdown** everywhere you write longer text: issue descriptions, comments, and wiki
pages. The same syntax works in all three. This page covers what's supported, plus two Specivo-specific
extras — `@username` mentions and `[[wiki links]]`.

## Headings

```markdown
# Heading 1
## Heading 2
### Heading 3
```

In the wiki, the page title is shown as the page heading already, so start your body at `##` rather than
repeating an `#` H1.

## Emphasis

| Markdown | Result |
|---|---|
| `**bold**` | **bold** |
| `*italic*` | *italic* |
| `***bold italic***` | ***bold italic*** |
| `` `inline code` `` | `inline code` |

## Lists

```markdown
- First item
- Second item
  - Nested item

1. Step one
2. Step two
```

Task lists work too:

```markdown
- [x] Done
- [ ] Not done yet
```

## Links and images

```markdown
[Link text](https://specivo.example.com)
![Alt text](https://specivo.example.com/image.png)
```

To attach and embed an image on an issue or wiki page, upload it as an
[attachment](../issues/attachments.md) first, then link to it.

## Blockquotes

```markdown
> A quoted line.
> Continues the same quote.
```

## Tables

```markdown
| Field    | Type   | Required |
|----------|--------|----------|
| subject  | string | yes      |
| due date | date   | no       |
```

Pipe tables render as styled tables. Use them for field lists and option matrices.

## Code blocks

Fence code with triple backticks and add a language hint for syntax highlighting:

````markdown
```python
def hello(name: str) -> str:
    return f"Hello, {name}"
```
````

The language hint (`python`, `bash`, `json`, `yaml`, `sql`, and so on) controls the highlighting colors.

## @username mentions

Type `@` followed by a username to mention someone. The mention links to their profile and **notifies
them**:

```markdown
@alex can you review this before the release?
```

Mentions work in issue descriptions and comments. Use them to pull a specific person into a discussion.

## Wiki cross-links

Inside the wiki, link to another page in the same project with double brackets:

```markdown
See [[Release Process]] for the steps.
```

You can link by title (`[[Release Process]]`) or by slug (`[[release-process]]`) — slugs are normalized, so
`[[My Page]]`, `[[my-page]]`, and `[[My_Page]]` all resolve to the same page. See
[Linking & history](../wiki/history-linking.md) for more.

!!! tip "Keep it simple"
    Stick to the elements above. Specivo renders standard Markdown plus the two extensions on this page —
    if a construct isn't listed here, don't assume it renders.
