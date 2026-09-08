# Importing from Redmine

Specivo can take over from a Redmine instance: projects and their hierarchy,
users and memberships, issues with their full history, wiki pages with every
revision, attachments and logged time.

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

## After the import

- Reset the imported accounts, or tell those people to use password recovery.
- Give any newly created roles their permissions.
- Check the flattened group memberships against how the group was used.

## Developing the importer

There is a Redmine fixture for working on this:

```
make redmine-fixture-up PROFILE=pg      # or PROFILE=mysql
make redmine-fixture-seed PROFILE=pg
make redmine-fixture-down PROFILE=pg
```

It runs Redmine 7.0.1 and seeds it through Redmine's own models, so the rows
are written the way a real instance writes them. The seed covers the cases the
importer has to handle: a subproject and an archived project, accounts in every
state, a group membership, a parent issue with subtasks, history of each shape,
relations including one across projects, a custom field of every format, a wiki
page with three revisions and a rename, attachments including a binary and a
file type Specivo would not accept from an upload today, non-Latin text, and
time entries.
