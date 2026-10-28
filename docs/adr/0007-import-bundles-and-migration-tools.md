# ADR-0007: Import Bundles and Separate Migration Tools

**Date:** 2026-10-28
**Status:** Accepted. Supersedes [ADR-0006](0006-redmine-importer-architecture.md) where it
places source adapters inside Specivo; its decisions about the load pipeline still hold.

## Context

ADR-0006 built the Redmine import into Specivo: an adapter read Redmine's database and emitted
the intermediate representation (IR) that the loaders consume, and a plugin hook,
`get_import_sources()`, let other adapters register the same way. That worked for one source,
but a second one (Jira) showed what it cost:

- Every source adapter ran inside Specivo's process, with Specivo's dependencies. The Redmine
  adapter alone pulled a MySQL driver and a Textile converter into the core install (the
  `importers` extra), for a command most installations run once, if ever.
- An adapter imported core's IR classes, so it had to be released in lockstep with Specivo,
  and it had to be written in Python against an internal API.
- Core's licence (AGPL) covered the adapters. Somebody writing an exporter for their own
  tracker should not have to take that on.
- Reading the source and loading the result were one run. An operator could not look at what
  was about to be imported, move it to another host, or load it in a maintenance window apart
  from the export.

## Decision

### 1. A file format is the only way into Specivo

An **import bundle** (`specivo-import-bundle`, format version 1) is a directory, or a `.zip` of
one: a `manifest.json`, a `lookups.json`, one JSON Lines file per entity type, and the
attachment bytes under `files/<sha256>`. [`docs/import-bundle/v1.md`](../import-bundle/v1.md) is
the complete contract, and the JSON Schemas beside it describe the same records
machine-readably.

Core reads bundles and nothing else: `python -m specivo.cli.import_data --source bundle --path …`
with `--validate` (checks only), `--dry-run` (the whole import in a transaction that is rolled
back), or neither. The bundle adapter (`specivo/importers/bundle/`) turns the bundle into IR, so
the pipeline, the loaders and the identifier map of ADR-0006 are unchanged. Reading another
system is no longer core's job: the Redmine adapter, its database drivers, its Textile converter,
the `importers` extra and the `get_import_sources()` plugin hook are removed.

A bundle is untrusted input. Core checks it fully before it loads anything (schema, references,
sizes and counts, zip entries, file types and hashes), refuses non-regular files, and never
imports administrator rights unless the operator asks for them (`--import-admins`). Credentials
named in a schema error are redacted.

### 2. Text refers to other records by placeholder

An importer cannot know what a record will be called in Specivo (project keys, suffixed logins,
stored file names). Text uses `{{issue:REF}}`, `{{user:REF}}`, `{{attachment:REF}}` and
`{{wiki:REF}}`, which core expands after everything is loaded. This replaces ADR-0006's
issue-reference rewrite, which had to understand Redmine's `#123`: core no longer interprets any
source's own reference syntax.

### 3. The identifier map is scoped by the bundle's source

The manifest names `source.system` (`redmine`, `jira`, …) and `source.instance` (the source
installation, such as its host name). Together they are the scope under which `import_id_map`
records every row an import created, as in ADR-0006. Two consequences:

- Loading a bundle of a scope that already has rows needs `--continue-import`, so an operator
  never adds to an earlier import by accident. Without it the load stops before writing.
- Rows imported directly before 0.11.0 are keyed by the masked database URL. `admin import-map
  rename` moves a scope to the instance name a bundle uses, after which bundles continue that
  import. `admin import-map list` shows the scopes and `purge` drops one.

### 4. References between bundles wait in a throwaway SQLite file

A source exported in parts (one project at a time) has issues that refer to issues of a bundle
loaded earlier or later. An issue placeholder outside the bundle is looked up in the same scope's
identifier map; what is still missing is written as plain text and remembered in a **links file**,
`specivo-links.<system>.<instance>.sqlite`, by default next to the bundle. Each later load
expands what has become resolvable, and the file is deleted when nothing is waiting.

A file, not a table, because this state only matters during a migration: it should not need a
schema migration, and deleting it after cut-over loses nothing but some links. Its rows are
signed with a key derived from `SECRET_KEY` and checked against the scope's identifier map, since
whoever supplies the bundles can supply the file too, and it is written only after the database
transaction it describes has committed. `admin import-map pending` and `resolve-pending` count
and link what is waiting outside an import.

### 5. Each migration tool is its own repository

| Repository | Package | What it is |
|---|---|---|
| `specivo/migration-sdk` | `specivo-migration-sdk` | Typed records, a streaming bundle writer, the same validation core runs, and the `specivo-bundle` command. Its only dependency is `jsonschema`. |
| `specivo/redmine-migration-tool` | `specivo-redmine-migration` | Reads a Redmine database (PostgreSQL or MySQL) and its files directory, writes a bundle. Carries what the direct import carried, under the same identifiers. |
| `specivo/jira-migration-tool` | `specivo-jira-migration` | The same for Jira. |

The tools and the SDK are licensed Apache-2.0 and never import Specivo. A tool in another
language needs nothing from them: the specification and the schemas are enough.

The tools are released as tagged GitHub releases, not on PyPI. A tool pins the SDK to a git tag
(`specivo-migration-sdk @ git+https://github.com/specivo/migration-sdk@vX.Y.Z`), and its CI
fails on a path or branch dependency.

### 6. Compatibility is the format version

Each tool and the SDK state the bundle format version they write and the oldest Specivo that
loads it (for format 1.0: Specivo 0.11.0). A minor format version only adds optional properties
or files, which an older reader ignores with a warning; a new major version is refused before
anything but the manifest is read. The SDK ships its own copy of the schemas, and its CI compares
them with those of a pinned Specivo release, so the two cannot drift apart unnoticed.

### 7. Attachments stay files

A bundle stores each distinct attachment once, under its SHA-256. The tools write a directory by
default, which needs no unpacking and can be copied with `rsync`. The files can be kept apart from
the records (`--files-path` on load), so a large files directory is not copied or zipped twice.

## Consequences

- Core is smaller: no source-specific code, no MySQL driver, no Textile converter. Its import
  tests write bundles from a test fixture instead of running a Redmine; tests that load bundles
  the SDK wrote run where the SDK's source is checked out beside core, and are skipped otherwise.
- An export can be inspected, validated with `specivo-bundle validate`, moved and loaded at a
  different time and place from where it was made. The load is the same for every source.
- A new source is a new repository against a published contract, and it can be released at its
  own pace. A change to the format is a change to the specification first.
- Three repositories to keep in step instead of one. The format version, the schema comparison
  in the SDK's CI, and each tool's end-to-end test against a real source are what hold them
  together.
- An installation that imported directly before 0.11.0 has to rename its scope once before
  loading a bundle. The upgrade notes say how.
- Operators get one more file to manage during a multi-part migration, the links file, which
  only works with the installation and `SECRET_KEY` that wrote it.

## Not Chosen

- **Keeping the plugin hook alongside bundles.** Two ways in means two things to secure and test,
  and an in-process adapter keeps every coupling listed under Context.
- **A database table for references between bundles.** It would need a migration for state that
  is thrown away after one migration.
- **An archive of all attachments.** A single large archive must be written, copied and read
  whole; a directory of hashed files can be copied incrementally and read directly.
- **Publishing the tools on PyPI.** They are installed by the operator doing a migration, from a
  tagged release; a public package index adds a release channel to secure for no gain.
