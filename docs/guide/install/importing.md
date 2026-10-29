---
description: Load data from another tracker into Specivo with an import bundle - check it, rehearse it, import it, add to it later, and inspect or retire an import.
---

# Importing from another tracker

Specivo does not read other trackers itself. A separate **migration tool** reads the old system and
writes an **import bundle**: a folder (or a `.zip`) of plain files holding the projects, issues, history,
wiki pages and attachments. Specivo then checks the bundle and loads it. Keeping the two apart means the
tool can be fixed and released without a new Specivo, and Specivo needs nothing from the old system once
the bundle exists.

This page covers the Specivo side, which is the same for every tool. Each tool has its own guide for the
export.

## Migration tools

| Moving from | Tool | Guide |
|---|---|---|
| Jira Cloud or Jira Data Center | `specivo-jira-migration` | [Jira migration tool docs](https://specivo.io/docs/jira-migration-tool/) |
| Redmine | `specivo-redmine-migration` | [Redmine migration tool](https://github.com/specivo/redmine-migration-tool/blob/main/docs/migrating-from-redmine.md) |

Another source needs a tool that writes the bundle format. It is described in
[`docs/import-bundle/v1.md`](https://github.com/specivo/specivo/blob/main/docs/import-bundle/v1.md) in the
Specivo repository, and the migration SDK writes it for you.

!!! note "Requires Specivo 0.11.0 or later"
    The bundle loader arrived in 0.11.0. It replaces the direct import of earlier versions; see
    [Upgrading to 0.11.0](upgrading.md#upgrading-to-0110).

## What a bundle can carry

A bundle can hold users and groups, projects and their members, versions, categories, sprints, custom
fields, issues with their comments and change history, relations and parent issues, watchers, attachments,
logged time and wiki pages with every revision. A tool fills in what its source has. A few details of how
Specivo loads them:

- **Custom fields** become [issue metadata](../metadata/index.md), shown by the name they had in the old
  system. Fields that hold a person or a version show that person's or version's name.
- **Labels** can arrive as [tags](../projects/tags.md), created in each issue's project.
- **Sprints** arrive with the state they had (planned, active or completed). A project can have only one
  active sprint, so a second active one is imported as planned and reported. A completed sprint's velocity is
  recorded from its issues.
- **An issue's remaining estimate** is stored next to its estimate. Specivo keeps it as given and does not
  derive it from time entries.
- **History** keeps its original authors and times. A change of status, assignee, version or similar shows
  by name, like any other change.
- **Text** is Markdown. References to issues, people and attachments are rewritten into links once every
  record exists, using the issue's new Specivo key.

Anything the tool could not carry is listed in the import report, so nothing disappears silently.

## Step 1: put the bundle where Specivo can read it

With Docker, the `api` container must see the folder. Mount it in `docker-compose.override.yml`:

```yaml
services:
  api:
    volumes:
      - ./imports:/import
```

Then restart the service (`docker compose up -d`) and copy the bundle into `./imports`. Let the folder be
writable by user ID `10001`, the user the app runs as: an import keeps a small **links file** next to the bundle (see [below](#exporting-in-parts)).
A bundle with large attachments can keep its files in a separate folder, which you give with `--files-path`.

## Step 2: check, rehearse, import

Run these in order. The first two change nothing.

```bash
# 1. Check the bundle itself: schema, references, file hashes. No database is touched.
docker compose exec api python -m specivo.cli.import_data \
    --source bundle --path /import/jira-export --validate

# 2. Rehearse: the whole import runs inside a transaction that is rolled back.
docker compose exec api python -m specivo.cli.import_data \
    --source bundle --path /import/jira-export --dry-run

# 3. Import for real.
docker compose exec api python -m specivo.cli.import_data \
    --source bundle --path /import/jira-export
```

Read the report from the dry run before you continue. It lists the project keys Specivo chose, statuses
whose category had to be guessed, accounts that need an email address, and everything that could not be
carried across. **Back up first**, then import. See [Backup & restore](backup-restore.md).

### Options worth knowing

| Option | What it does |
|---|---|
| `--project IDS` | Import only these source projects (comma-separated source ids). Subprojects come with their parent. |
| `--project-key-map ID=KEY,...` | Choose the Specivo project key for a source project instead of the derived one. |
| `--merge-duplicate-statuses` | Reuse an existing status, tracker or priority of the same name instead of creating a second one. |
| `--continue-import` | Add to an earlier import of the same source (see below). |
| `--resume RUN_ID` | Continue a run that stopped. Each phase commits on its own, so what finished stays. |
| `--links-file PATH` | Where to keep the links file. |
| `--files-path DIR` | Read attachment files from this folder instead of the bundle's own `files/`. |
| `--import-passwords` | Let imported accounts keep their old password until first sign-in. Needs a matching [password hasher](password-hasher-plugins.md). Administrators always set a new one. |
| `--import-admins` | Make the source's administrators administrators here too. Otherwise they arrive as ordinary accounts. |
| `--flatten-excess-depth` | Re-parent projects nested deeper than Specivo allows. |
| `--report-json PATH` | Also write the report to a file. |
| `--strict` | Treat every warning as a failure. For testing a tool, not for a real import. |

`python -m specivo.cli.import_data --source bundle --help` lists all of them, including the size limits for
untrusted bundles.

!!! tip "Accounts without an email address"
    Some sources do not reveal everyone's email address. Specivo gives such an account a placeholder
    address that never receives mail, and lists it in the report so you can fill in the real one.

## Running it again, and adding to it later

Every record in a bundle has a stable identifier from the source. Specivo remembers which source record
became which Specivo row, under a **scope**: the source system and the instance named in the bundle's
manifest.

- Loading the same bundle again creates nothing new.
- A **later export of the same installation**, loaded with `--continue-import`, adds the records created
  since. Use this to import early and catch up just before you switch over. Records already imported are
  skipped, not updated.
- Without `--continue-import`, Specivo refuses a bundle whose scope already has rows, so a bundle cannot
  attach itself to another import by naming its source.

## Exporting in parts

A big installation can be exported one project (or a few) at a time. Issues in different parts can still
link to each other: a comment that mentions an issue, a relation between two issues, a sub-task whose
parent is in another part.

Load the parts one after the other, in any order, with `--continue-import` from the second one on and the
same `--links-file` each time. Whatever points at an issue that has not arrived yet waits in the links file:

- a mention is written as plain text first and becomes a link when the other part is loaded;
- a relation is created, and a parent is set, once both issues exist.

The file is deleted when nothing waits. Text that someone edited after the import is left alone.

## Inspect and tidy up an import

The `admin` command can show and manage what imports remember:

```bash
docker compose exec api python -m specivo.cli.admin import-map list
docker compose exec api python -m specivo.cli.admin import-map pending --links-file /import/specivo-links.SYSTEM.INSTANCE.sqlite
docker compose exec api python -m specivo.cli.admin import-map resolve-pending --links-file /import/... --yes
```

| Command | What it does |
|---|---|
| `import-map list` | Lists each scope with its row counts and when it last wrote. |
| `import-map pending` | Counts the texts, relations and parents still waiting in a links file. Reads only. |
| `import-map resolve-pending` | Links whatever can be linked now. Without `--yes` it only reports. |
| `import-map rename` | Moves a scope to a new system or instance, so the next import finds its rows. |
| `import-map purge` | Deletes a scope's rows after you have switched over. Imported records stay, but **later imports of that source would create everything again**. |

`rename` and `purge` are dry runs unless you add `--yes`.

!!! warning "Purge only when the old system is retired"
    The scope's rows are how Specivo recognises what is already imported. After a `purge` you cannot add
    a later export of that source. Delete the scope's links file too.

## After the import

- Sign in as an administrator and check a few projects: issue counts, an issue with a long history, a wiki
  page, an attachment.
- Fill in the real email address for accounts that were given a placeholder, and tell people to set a new
  password (or sign in with [single sign-on](single-sign-on/index.md)).
- Check the roles of the imported members in each project.
- Search indexing for the imported records runs at the end of the import. See
  [Semantic search](semantic-search.md).
