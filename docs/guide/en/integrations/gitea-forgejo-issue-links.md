---
description: Make issue references in Gitea and Forgejo link to Specivo — set Specivo as the repository's external issue tracker, with the alphanumeric, numeric or regular expression number format.
---

# Issue links from Gitea and Forgejo

Gitea and Forgejo can treat another system as a repository's **external issue tracker**. Point it at
Specivo and every issue reference in the forge's commit messages, pull requests and comments
(`ACME-12`, or `#12`) becomes a link to the issue in Specivo, and the **Issues** tab of the
repository can open your Specivo project instead of the forge's own issues. Gitea and Forgejo have
the same settings for this, in slightly different places; the names below are Gitea's English
labels (Forgejo writes them in sentence case, such as "Use external issue tracker").

This works in the other direction from a [repository integration](repositories.md), which sends
the forge's commits and pull requests *to* Specivo. The two go well together.

## Where Specivo's issues live

Every issue has a page at `/issue/{key}/` on your Specivo address:

```text
https://specivo.example.com/issue/ACME-12/
```

- The key is not case-sensitive in this address: `/issue/acme-12/` opens the same issue.
- An issue that was moved to another project still opens under its old key.
- After an administrator **renames a project key**, issue pages answer only under the new key.
  Update the forge's settings below when that happens.
- If your instance uses a `STEALTH_PREFIX`, it comes before `/issue/`.
- The forge only builds links; Specivo decides who may see the issue. People without access get
  the sign-in page or "not found", as for any other link.

Do not link to `/issue/12/` with a bare number: a number on its own is Specivo's internal issue id,
not the number in `ACME-12`, and would open another issue.

## Set it up

You need administrator rights on the repository. Open the repository's **Settings**, then:

- **Gitea:** the **Advanced Settings** section of the main settings page, under **Issues**.
- **Forgejo:** **Units** in the settings sidebar, then the **Issues** section.

How you switch the external tracker on depends on the version:

- **Forgejo, and Gitea up to 1.27:** tick **Enable Repository Issue Tracker**, then choose
  **Use External Issue Tracker** instead of **Use Built-In Issue Tracker**.
- **Gitea 28 and later:** the two trackers are separate boxes. Tick **Use External Issue Tracker**
  and untick **Use Built-In Issue Tracker**. While the built-in tracker stays on, the **Issues** tab
  keeps showing the forge's own issues and `#12` keeps linking to them, so the numeric format below
  does not reach Specivo; alphanumeric and regular expression references still do.

Then fill in:

| Setting | Value |
|---|---|
| **External Issue Tracker URL** | Where the **Issues** tab should go: the project's issue list, `https://specivo.example.com/projects/ACME/issues/`. |
| **External Issue Tracker URL Format** | The issue address, with a placeholder. The forge offers `{user}` (the repository owner), `{repo}` (the repository name) and `{index}` (the issue reference). Specivo only needs `{index}`; see the formats below. |
| **External Issue Tracker Number Format** | **Alphanumeric**, **Numeric** or **Regular Expression**; see below. |

Save the settings. The forge turns references into links when it shows the text, so commit
messages and comments written before the change link too.

### Alphanumeric: references like `ACME-12`

Choose this when your team writes full Specivo keys, which is also what a
[repository integration](repositories.md#how-commits-and-pull-requests-name-an-issue) reads.

| Setting | Value |
|---|---|
| URL Format | `https://specivo.example.com/issue/{index}/` |
| Number Format | Alphanumeric |

`{index}` is the whole reference, `ACME-12`, so one repository can link to issues in several
projects.

The forge recognises an alphanumeric reference only when the key is **1 to 10 upper-case letters**
and the number does not start with `0`, written in upper case (`acme-12` is not linked) and standing
on its own: followed by a space, the end of the line, `.`, `,`, `:`, a closing bracket or a quote,
not by other punctuation such as `;` or `!`. Specivo keys may also contain digits and be longer
(`WEB2`, `INFRASTRUCTURE`); references with such keys are not linked in this format. Use
**Regular Expression** for them.

### Numeric: references like `#12` with a fixed key

Choose this when the repository belongs to one Specivo project and you prefer to write `#12`.

| Setting | Value |
|---|---|
| URL Format | `https://specivo.example.com/issue/ACME-{index}/` |
| Number Format | Numeric |

`{index}` is the number only, so the URL Format must carry the key and the hyphen. Every `#12` then
opens ACME-12; issues in other projects cannot be linked.

The forge numbers its own pull requests with `#` too. With a numeric external tracker, `#12`
always means the Specivo issue; write `!12` for the forge's pull request 12. Because the two are
easy to mix up, prefer the alphanumeric format when you can.

### Regular Expression: any other key

Choose this for keys with digits or more than ten letters, or to match only certain keys.

| Setting | Value |
|---|---|
| URL Format | `https://specivo.example.com/issue/{index}/` |
| Number Format | Regular Expression |
| Regular Expression Pattern | `\b(WEB2-[0-9]+)\b` |

The forge replaces `{index}` with what the **first group in parentheses** captures, here
`WEB2-12`. To accept several keys, list them: `\b((?:WEB2|INFRASTRUCTURE)-[0-9]+)\b`. To capture the
number only, put the key in the URL Format as in the numeric format:
`https://specivo.example.com/issue/WEB2-{index}/` with the pattern `\bWEB2-([0-9]+)\b`.
