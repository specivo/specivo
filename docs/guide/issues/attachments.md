---
description: Attaching files to issues, wiki pages, and comments in Specivo — the optional searchable description, the operator-set size limit, and no built-in type restriction.
---

# Attachments

You can attach files to **issues**, **wiki pages**, and **comments** — screenshots, logs, design
files, spreadsheets, anything that helps explain or document the work.

![Attachments on an issue](../assets/img/issue-attachments.webp)

## Attach a file

Use the attachment area on the issue (or wiki page, or comment) to upload a file. Specivo keeps each
file's original filename, size, and type so it downloads back exactly as you uploaded it.

## Add a description

Each attachment has an optional **description** field. It's worth filling in: descriptions are
**searchable**, so "production error log, 2 Mar" turns up in [search](finding.md) later even though the
filename was something like `app-2024.log`.

## Who can open an attachment

An attachment is visible to exactly the people who can see what it is attached to:

- a file on an **issue** opens for anyone who can see that issue — so a file on a private issue, or on
  an issue your role's issue visibility hides from you, stays hidden too;
- a file on a **wiki page** opens for anyone who can read the project's wiki;
- a file on an issue **comment** opens for anyone who can see the issue, except on a private note,
  where it opens for administrators only.

To anyone else, a hidden attachment looks exactly like one that doesn't exist: its download link answers
"not found".

## Link to an attachment by filename

In Markdown you can point at an attachment by its filename alone. Write an image to show it inline or a
link to any other file, and Specivo turns it into the attachment's download link when the page renders:

```markdown
![chart](chart.png)
See [the spec](spec.pdf) and the [crash log](crash.log).
[release notes](<release notes.pdf>)
```

- The filename has to match exactly, including upper and lower case. A name with spaces can be written
  in angle brackets, as above, or URL-encoded, as `release%20notes.pdf`.
- Only a bare filename is looked up. A full URL (`https://…`), a path containing `/`, an anchor (`#…`)
  and a `mailto:` link are left as you wrote them. A filename with no matching attachment is left alone
  too.
- In an **issue's description and comments**, the name is looked up among the files attached to **that
  issue** and its comments. A file on another issue doesn't resolve there, even if it has the same name.
- On a **wiki page**, the name is looked up among the project's issue and wiki attachments. If several
  share the name, the newest wins.
- Only attachments **you can open** take part. A reader who can't see the issue holding `chart.png`, or
  a visitor reading without an account, sees the filename but gets no image or link.

The editor's preview resolves filenames the same way, so it shows what the saved text will look like.

## Size and type

- **Maximum upload size** is set by the operator who runs your Specivo server, so the limit depends on
  your installation.
- There is **no built-in file-type restriction** — Specivo doesn't block files by extension.

!!! warning "Upload rejected?"
    If an upload fails, the file is most likely larger than the maximum size your operator configured.
    Ask them whether the limit can be raised, or attach a smaller or compressed version.
