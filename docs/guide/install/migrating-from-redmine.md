---
description: Move a Redmine instance into Specivo — projects, users, groups, issues with their history, wiki revisions, attachments and logged time — starting with a dry run.
---

# Migrating from Redmine

!!! warning "The direct Redmine import has been removed"
    Specivo no longer reads a Redmine database itself, and `import_data --source redmine`,
    `make import-redmine` and the `specivo[importers]` extra no longer exist. Redmine is imported
    with the [Redmine migration tool](https://github.com/specivo/redmine-migration-tool), which
    writes an import bundle that `python -m specivo.cli.import_data --source bundle --path ...`
    loads. The sections below still describe the removed command and are being rewritten; the
    parts about what is carried across, `--dry-run`, `--resume` and importing again apply to a
    bundle unchanged.

Specivo can take over from a Redmine instance: projects and their hierarchy,
users, groups and memberships, issues with their full history, wiki pages with
every revision and the redirects left behind by page renames, attachments and
logged time.

Groups arrive as groups. A Redmine group becomes a Specivo user group holding
the same people, and a role that group held on a project stays a grant to the
group rather than being copied onto each member — so adding somebody to the
group afterwards gives them that access, and removing them takes it away, the
way it did in Redmine. Redmine's own bookkeeping rows for what each member
inherited are not imported: Specivo works that out from the group when it
checks a permission.

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

`make import-redmine` runs `python -m specivo.cli.import_data --source redmine`
inside the `api` container. Without `make`, run that yourself with
`docker compose exec api`, followed by the same options.
`--source redmine --help` lists every option.

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

## Importing again later

Specivo remembers which Redmine record became which Specivo row, under a key
made of the source system (`redmine`) and the instance name: `--source-instance`
if you gave one, otherwise the source database URL with its password masked
(`postgresql://redmine:***@db.internal/redmine`). A later import under the same
key adds only what is new and skips the rest.

An import bundle written by a Redmine export tool is keyed the same way, by the
system and instance its manifest names. So a bundle can carry on from a direct
import, as long as both use the same instance name. If your earlier direct
import ran without `--source-instance`, its key is the masked URL. Before you
load the first bundle, move it to the name the bundle uses:

```
# the keys in this database, with what each holds
docker compose exec api python -m specivo.cli.admin import-map list

# check the move (a dry run: nothing changes without --yes)
docker compose exec api python -m specivo.cli.admin import-map rename --system redmine \
  --from 'postgresql://redmine:***@db.internal/redmine' --to tracker.example.com
# then do it
docker compose exec api python -m specivo.cli.admin import-map rename --system redmine \
  --from 'postgresql://redmine:***@db.internal/redmine' --to tracker.example.com --yes
```

The move is one transaction. It is refused if the new key already holds rows.

A bundle that names a key with rows is refused unless you say it continues that
import, so a bundle cannot quietly attach its records to an import it does not
belong to. Once the keys line up, load it with `--continue-import`:

```
docker compose exec api python -m specivo.cli.import_data --source bundle \
  --path /import/export --continue-import --dry-run
```

The dry run should report that the source was imported before and that nothing
is created except records added in Redmine since. Then run it without
`--dry-run`. Records without an author still belong to the `redmine-import`
account.

### One project at a time, in any order

An export tool can write one bundle per project. Load them in any order, the
first as usual and every later one with `--continue-import`. A reference to an
issue of a project that is not loaded yet is written as plain text at first
(`ACME-15` cannot be known yet, so the text shows the label or the Redmine
number), and becomes a link when that project's bundle is loaded.

Specivo remembers those texts in a **links file**, a small SQLite file next to
the bundle: `specivo-links.redmine.<instance>.sqlite`. Every load of the same
source has to see the same file, so:

- When imports run in Docker, keep the bundles **and the links file** in a
  folder mounted from the host, not inside the container, where the file would
  be lost with it.
- If the bundles are on a read-only mount, put the links file on a writable one
  with `--links-file /import/state/links.sqlite`, and pass the same option to
  every load. Without a writable location the import still succeeds, with a
  warning that its references will not be linked later.

A text somebody edits in Specivo between two loads is never overwritten: it
keeps the plain-text reference, and the report says which texts were left
alone. To see what is still waiting, or to link it without a new import:

```
docker compose exec api python -m specivo.cli.admin import-map pending \
  --links-file /import/specivo-links.redmine.tracker.example.com.sqlite
# a dry run, then for real
docker compose exec api python -m specivo.cli.admin import-map resolve-pending \
  --links-file /import/specivo-links.redmine.tracker.example.com.sqlite --yes
```

The file deletes itself once nothing is waiting. It is a throwaway: if you lose
it, or delete it after the migration, the remaining references simply stay
plain text. It only works with the Specivo that wrote it: each entry is signed
with a key derived from `SECRET_KEY`, so after changing `SECRET_KEY`, or on
another installation, its entries are ignored (with a warning) and those
references stay plain text. Keep it a regular file: a symbolic link is refused.
Keep its folder writable by you only; anyone else who can write there could
still tamper with the file while an import opens it.

### After cut-over

Once Redmine is retired and nothing more will be imported from it, you can
delete what the import remembered about it:

```
# a dry run: counts the rows
docker compose exec api python -m specivo.cli.admin import-map purge \
  --system redmine --instance tracker.example.com
docker compose exec api python -m specivo.cli.admin import-map purge \
  --system redmine --instance tracker.example.com --yes
```

The imported issues, pages and accounts stay. But those rows are how a later
import recognises what is already here, so after a purge **no delta import of
that source is possible**: one would create everything a second time. Delete
the links file too.

## Keeping people's passwords

By default every imported account arrives without a usable password (see
[What does not come across](#what-does-not-come-across)). If you would rather
people keep signing in with the password they already have, the importer can
bring it over. You have to ask for it twice: once in the configuration and once
on the command line.

1. Enable Redmine's password format in `.env` and restart the stack:

    ```bash
    LEGACY_PASSWORD_ALGORITHMS=["redmine_sha1"]
    ```

2. Run the import with `--import-passwords`:

    ```
    make import-redmine ARGS='\
      --source-db-url postgresql://redmine:secret@db.internal/redmine \
      --source-files-dir /mnt/redmine-files \
      --import-passwords'
    ```

If the algorithm isn't enabled, an import with `--import-passwords` stops before
it writes anything. Without the flag, every account takes the reset path even
when the algorithm is enabled.

What happens to each account:

- **Local Redmine accounts keep their password.** Redmine stores a salted SHA1
  digest. Specivo never stores that digest on its own: it wraps it in bcrypt at
  import, so a copy of the database holds nothing weaker than a native Specivo
  hash. At the person's first sign-in the stored value is replaced with an
  ordinary bcrypt hash of their password. They don't notice anything, and the
  upgrade is recorded in the security audit log. If Redmine had asked an
  account to change its password at next sign-in, Specivo asks too.
- **Administrators always set a new password.** An administrator account is the
  one most worth taking over, so its Redmine password is never carried over. The
  report lists these accounts under *administrators asked to set a new
  password*.
- **LDAP accounts and accounts without a local password** take the reset path,
  as they would without the flag. Redmine never checks their local hash, so
  there is nothing to keep. The report counts LDAP accounts separately.

Accounts that kept their password are listed in the report under *accounts that
keep their existing password*; everyone else is under *accounts that will be
asked to set a password at first sign in*.

Each imported password costs one bcrypt at the normal cost, about a quarter of
a second, which adds roughly four minutes per thousand accounts to the import.

Redmine's SHA1 scheme is weak, and wrapping it doesn't make a weak password any
stronger. Once most people have signed in, retire the old hashes:

```bash
# Who still holds an imported password, with their last sign-in:
docker compose exec api python -m specivo.cli.admin legacy-passwords

# Make them use password recovery from now on:
docker compose exec api python -m specivo.cli.admin legacy-passwords expire --yes
```

Or set a date in `.env`, after which no imported password works and the
remaining people use password recovery:

```bash
LEGACY_PASSWORD_HASHES_EXPIRE_AT=2027-03-31T00:00:00Z
```

Either way, the sign-in page shows the usual "Invalid login or password", so it
doesn't reveal which accounts are affected. Coming from something other than
Redmine? See [Writing a password hasher plugin](password-hasher-plugins.md).

## Large migrations

A Redmine with years of attachments can hold many gigabytes of files. Keep
them out of any archive: export the bundle to a folder, and copy the folder
with `rsync`, which can resume and skips files already copied. Files are
named by their content hash, so a later export only adds new ones and a second
`rsync` only moves those.

1. Export to a folder (the export tool's default; a zip is only for small
   exports and trial runs).
2. Copy it to the Specivo host, records and files together or apart:

   ```
   rsync -a --partial export/ specivo-host:/srv/import/export/
   ```

   The records are small; if you would rather move them as a zip, zip only
   the records and copy `files/` on its own to, say, `/srv/import/files/`.
3. Make the folder visible to the API container, then check it. A read-only
   mount is enough for the bundle, but the import keeps its links file next to
   it (see [One project at a time](#one-project-at-a-time-in-any-order)):
   mount it writable, or point `--links-file` at a writable folder. Nothing is
   written by the check, and the database is not touched:

   ```
   docker compose exec api python -m specivo.cli.import_data --source bundle \
     --path /import/export --validate
   # records in a zip, files apart:
   docker compose exec api python -m specivo.cli.import_data --source bundle \
     --path /import/data.zip --files-path /import/files --validate
   ```

   A missing file, one whose bytes do not match its name, or a symbolic link
   among the files is reported with the record it belongs to.
4. Rehearse with `--dry-run` instead of `--validate`, and read the report.
5. Import: the same command without either.

For a later delta, export again into the same folder, `rsync` again, and run
the same steps with `--continue-import` added to the dry run and the import.
Records and files already imported are recognised and skipped; only what is new
is created and copied.

## Useful options

| Option | What it does |
|---|---|
| `--project 1,2` | Import only these Redmine project ids. Subprojects come with their parent. |
| `--merge-duplicate-statuses` | Reuse an existing status, tracker or priority of the same name instead of creating a second one. |
| `--flatten-excess-depth` | Import projects nested deeper than Specivo allows by re-parenting them higher up. |
| `--cf-key-map "Story Points=points"` | Choose the metadata key a custom field is stored under. |
| `--source-instance name` | Name this Redmine, so two installations can be imported into one Specivo. |
| `--import-passwords` | Let accounts keep their Redmine password. Needs `redmine_sha1` in `LEGACY_PASSWORD_ALGORITHMS`; see [Keeping people's passwords](#keeping-peoples-passwords). |
| `--report-json path` | Write the report as JSON as well as printing it. |
| `--links-file path` | For a bundle: where to keep the [links file](#one-project-at-a-time-in-any-order) (default: next to the bundle). |
| `--import-admins` | Make Redmine's administrators administrators in Specivo too. Without it they arrive as ordinary accounts and the report lists them, so you can grant the rights by hand. |
| `--batch-size n` | Rows read per page from the source (default 500). Lower it if the source database is memory-constrained; raise it to reduce round trips on a fast connection to a large instance. |

## What does not come across

Some things have no Specivo equivalent. The importer says so rather than
guessing:

- **Administrator rights, unless you ask for them.** Redmine's administrators
  are imported as ordinary accounts unless you pass `--import-admins`. Either
  way the report lists them, along with every locked or not yet active account
  and every account that signed in through LDAP.

- **Passwords, unless you ask for them.** Without `--import-passwords` (see
  [Keeping people's passwords](#keeping-peoples-passwords)), every imported
  account arrives with a password nobody holds, and is marked as owing one.
  Administrators and LDAP accounts are always treated this way. Give an account
  a password from **Administration → Users** and the person is asked to replace
  it the first time they sign in, so the one you typed is not the one they
  keep. If mail is configured they can instead use password recovery and set
  their own straight away. Either way the logins are in the report.
- **Modules Specivo does not have** — repositories, forums, news, documents,
  calendars and Gantt.
- **Custom fields on anything but issues.** Fields on users, projects, versions
  and time entries are counted and dropped.
- **Role permissions.** Redmine's permission names are its own, so translating
  them would quietly widen or narrow access. Roles are matched by name against
  the ones you already have, and any that are new arrive with no permissions
  for an administrator to fill in.
- **Write access for people without a membership.** Redmine's *Non member*
  role is mapped onto Specivo's built-in *Non member* role, which is read-only
  and is not changed by the import. Anything Redmine's role allowed beyond
  reading issues (adding issues and notes, for instance) is not carried over,
  and the report lists every such permission, along with any per-project
  *Non member* override, which Specivo has no equivalent for.
- **Per-project priorities and activities**, which Specivo keeps global.
- **Who logged a time entry**, as distinct from whose time it is. Redmine
  tracks both; Specivo keeps only whose time it is, so an entry a manager
  logged on someone else's behalf keeps the person it belongs to.

## What gets adjusted

A few things come across but not unchanged, and each is worth knowing about
before you look at real numbers:

- **Role visibility and private trackers** come across as they were: a role's
  *users visibility* and *time entries visibility*, and a tracker's *private by
  default*. A role or tracker matched to one you already had is not changed,
  so when the Redmine one was narrower — a role that saw only its own time,
  say — the import reports it and you decide whether to restrict yours.
  Imported issues keep their own private flag either way.
- **Logins and email addresses that collide with an existing account** are
  suffixed (`jsmith-2`, `jsmith+2@example.com`) rather than skipped, and every
  suffixed one is listed in the report.
- **A group whose name is already taken** is suffixed the same way
  (`Platform Team-2`), and the suffixing is reported. Specivo requires group
  names to be unique ignoring case and Redmine does not, so this covers both a
  name your Specivo already uses and two Redmine groups differing only in case.
  The imported group is always a new one: adding Redmine's members and grants
  to a group you already had would hand the people in it access nobody decided
  to give them.
- **Logged time** is rounded to two decimal places, since that is all Specivo
  stores. Floating-point noise from Redmine's own storage (`7.5` coming back
  as `7.499999999999999`) is rounded silently; a rounding that drops precision
  someone actually entered is reported.
- **A custom field that is required in Redmine but has existing issues with no
  value for it** is imported as optional instead. Keeping it required would
  make the imported data invalid against its own schema on arrival.

## Anonymous access

Redmine can let people read public projects without signing in. Specivo can
too, but per project and behind an instance-wide switch that is off by default.
The import carries Redmine's setting over **without ever turning that switch
on**, so nothing becomes readable without an account until you decide it
should:

- Anonymous access is carried over only if Redmine's **Authentication
  required** setting (`login_required`) is explicitly off. Redmine does not
  store a setting it still has at its default, so when it is absent the import
  treats the source as requiring sign-in and carries nothing over. Guessing the
  other way would publish your tracker.
- Each **public**, non-archived project is then opted in to what Redmine's
  *Anonymous* role, or a per-project *Anonymous* override on that project,
  allowed: reading issues, reading the wiki, or both, and only where the
  matching module is enabled. Private projects never are. Anonymous
  permissions beyond those two are listed in the report.
- Every opt-in is written to the security audit log.
- A second run does not opt a project in again, so a project you have since
  closed to anonymous visitors stays closed.
- If anonymous access is **already on** in this Specivo, the import opts
  nothing in, because that would make the projects readable at once. It lists
  them instead, for you to opt in by hand.

The report has a section listing the projects that become readable without an
account once the switch is on, with what each one allows. To turn it on,
review that list, then use **Administration → Settings → Anonymous access**.
Turning it on asks you to confirm exactly those projects. A project's own
opt-in is under its **Settings**, and only instance administrators can change
it.

## After the import

- Give the accounts that owe a password a way in: set a password for each from
  **Administration → Users** and pass it on, or tell those people to use
  password recovery. A password you set is replaced at first sign-in, so
  neither route leaves an account whose password somebody else knows. If you
  imported passwords, that is only the administrators and LDAP accounts.
- Give any newly created roles their permissions.
- If the report lists projects that become readable without an account,
  review them and turn anonymous access on yourself when you are ready (see
  [Anonymous access](#anonymous-access)). The import never does.
- Check the imported groups under **Administration → Groups**, and any that were
  suffixed on a name collision in particular — a suffixed group is a second one
  next to the group that already held the name, which may not be what you want.
