---
description: Move a Redmine instance into Specivo — projects, users, issues with their history, wiki revisions, attachments and logged time — starting with a dry run.
---

# Migrating from Redmine

Specivo can take over from a Redmine instance: projects and their hierarchy,
users and memberships, issues with their full history, wiki pages with every
revision and the redirects left behind by page renames, attachments and
logged time.

The importer reads Redmine's database directly and copies its attachment files.
It does not use Redmine's REST API, which cannot see private notes, deleted
users or complete history, and is far slower. Nothing is written to the source
— a read-only database user and a read-only mount of the files directory are
enough.

## Before you start

You need three things:

- A connection URL for Redmine's database. Both MySQL and PostgreSQL work; the
  driver in the URL is optional and filled in for you.
- Redmine's attachment directory, mounted where the importer can read it.
  Without it, attachments are skipped and everything else still imports.
- A Specivo instance to import into. It does not have to be empty.

MySQL sources need one extra dependency: `uv pip install 'specivo[importers]'`.

## Always start with a dry run

```
make import-redmine ARGS='\
  --source-db-url postgresql://redmine:secret@db.internal/redmine \
  --source-files-dir /mnt/redmine-files \
  --dry-run'
```

A dry run does the whole import and rolls it back, so nothing is written and no
files are copied. Its report is the point: it lists the decisions the importer
made that you may want to change before they are permanent.

Three parts of that report are worth reading closely.

**Project keys.** Redmine identifies an issue as `#123`; Specivo identifies it
as `ACME-15`, and Redmine has no per-project prefix to take that from. A key is
derived from each project's identifier, and every one is listed. To choose your
own:

```
--project-key-map acme-app=ACME,internal-ops=OPS
```

**Status categories.** Redmine records only whether a status closes an issue.
Specivo groups statuses into backlog, active, done and closed, so the rest is
inferred from the name. Correct any of it:

```
--status-category-map "Awaiting Review=active,Verified=done"
```

**Anything that could not be carried across.** Modules with no equivalent,
custom fields defined on users or projects, attachments whose file is missing
from disk. Each is listed with enough detail to check.

## Running the import

Drop `--dry-run` when the report looks right:

```
make import-redmine ARGS='\
  --source-db-url postgresql://redmine:secret@db.internal/redmine \
  --source-files-dir /mnt/redmine-files \
  --project-key-map acme-app=ACME \
  --report-json /tmp/import-report.json'
```

The run prints an identifier. If the import is interrupted, pass it back:

```
make import-redmine ARGS='--source-db-url ... --resume 79309b3d-25a0-42df-a7ec-cf0be57b1ca1'
```

Each phase commits as it finishes, so a resumed run keeps what landed and
continues. Re-running an import that already finished is safe: everything is
recognised and skipped.

## Useful options

| Option | What it does |
|---|---|
| `--project 1,2` | Import only these Redmine project ids. Subprojects come with their parent. |
| `--merge-duplicate-statuses` | Reuse an existing status, tracker or priority of the same name instead of creating a second one. |
| `--flatten-excess-depth` | Import projects nested deeper than Specivo allows by re-parenting them higher up. |
| `--cf-key-map "Story Points=points"` | Choose the metadata key a custom field is stored under. |
| `--source-instance name` | Name this Redmine, so two installations can be imported into one Specivo. |
| `--report-json path` | Write the report as JSON as well as printing it. |
| `--batch-size n` | Rows read per page from the source (default 500). Lower it if the source database is memory-constrained; raise it to reduce round trips on a fast connection to a large instance. |

## What does not come across

Some things have no Specivo equivalent. The importer says so rather than
guessing:

- **Passwords.** The two systems hash differently and nothing is portable.
  Every imported account gets an unusable password and is listed in the report;
  those people use password recovery, or an administrator resets them.
- **Groups.** Specivo cannot hang project roles off a group, so a group's grant
  is expanded into the same grant for each of its members. The resulting access
  matches Redmine exactly; what is lost is the knowledge that it came from a
  group.
- **Modules Specivo does not have** — repositories, forums, news, documents,
  calendars and Gantt.
- **Custom fields on anything but issues.** Fields on users, projects, versions
  and time entries are counted and dropped.
- **Role permissions.** Redmine's permission names are its own, so translating
  them would quietly widen or narrow access. Roles are matched by name against
  the ones you already have, and any that are new arrive with no permissions
  for an administrator to fill in.
- **Per-project priorities and activities**, which Specivo keeps global.
- **Who logged a time entry**, as distinct from whose time it is. Redmine
  tracks both; Specivo keeps only whose time it is, so an entry a manager
  logged on someone else's behalf keeps the person it belongs to.

## What gets adjusted

A few things come across but not unchanged, and each is worth knowing about
before you look at real numbers:

- **Logins and email addresses that collide with an existing account** are
  suffixed (`jsmith-2`, `jsmith+2@example.com`) rather than skipped, and every
  suffixed one is listed in the report.
- **Logged time** is rounded to two decimal places, since that is all Specivo
  stores. Floating-point noise from Redmine's own storage (`7.5` coming back
  as `7.499999999999999`) is rounded silently; a rounding that drops precision
  someone actually entered is reported.
- **A custom field that is required in Redmine but has existing issues with no
  value for it** is imported as optional instead. Keeping it required would
  make the imported data invalid against its own schema on arrival.

## After the import

- Reset the imported accounts, or tell those people to use password recovery.
- Give any newly created roles their permissions.
- Check the flattened group memberships against how the group was used.
