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

On a wiki page, an image written by bare filename, such as `![chart](chart.png)`, is looked up among the
project's issue and wiki attachments **that you can open**. If several share the name, the newest of
those wins. A reader who can't see the issue holding `chart.png` sees no image there.

## Size and type

- **Maximum upload size** is set by the operator who runs your Specivo server, so the limit depends on
  your installation.
- There is **no built-in file-type restriction** — Specivo doesn't block files by extension.

!!! warning "Upload rejected?"
    If an upload fails, the file is most likely larger than the maximum size your operator configured.
    Ask them whether the limit can be raised, or attach a smaller or compressed version.
