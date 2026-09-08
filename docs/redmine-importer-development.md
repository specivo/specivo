# The Redmine importer

Operator-facing instructions live in the user guide, under
[Migrating from Redmine](guide/install/migrating-from-redmine.md). This page is
for changing the importer itself: its architecture, how to add a new source,
what each translation rule does, and how the test fixture works.

The reasoning behind the decisions below — why the write path goes through
Specivo's own services, why idempotency lives in a database table, why phases
commit individually — is recorded in
[ADR-0006](adr/0006-redmine-importer-architecture.md). This page is the "what"
and "where"; the ADR is the "why".

## Package layout

```
specivo/importers/
  core/
    ir.py            intermediate representation dataclasses, EntityType vocabulary
    source.py         SourceAdapter protocol + adapter registry
    converter.py      ContentConverter protocol, ConversionContext, PassthroughConverter
    pipeline.py        ImportPhase (19 phases), ImportPipeline, ImportOptions, ImportSummary, PhaseContext
    id_map.py          ImportIdMap over the import_id_map table
    backdate.py         restores original created_at/updated_at after a service creates a row
    progress.py         ProgressReporter protocol, CliProgressReporter, NullProgressReporter
  load/
    registry.py         wires loaders onto phases — the one file that says what a full import consists of
    lookup_loader.py     statuses, trackers, priorities, activities, roles
    user_loader.py        users, the import service account, group membership
    project_loader.py     projects, versions, categories, memberships, custom-field schemas
    issue_loader.py        issues, journals, relations, watchers, custom-field reference resolution
    wiki_loader.py          wiki pages, revisions, watchers, redirects
    attachment_loader.py    file copy for issues and wiki pages
    time_entry_loader.py     logged time
    reference_rewrite.py      #123 -> KEY-N rewriting, once every issue exists
    search_index.py           wiki link graph rebuild + search backfill, run once at the end
  redmine/
    db.py               hand-declared SQLAlchemy Core tables for 32 Redmine tables, engine factory, keyset paging
    extract.py            pure row-dict -> IR functions and all Redmine-specific translation rules
    adapter.py             RedmineSourceAdapter — the only module that knows Redmine's schema
    textile_convert.py      Textile -> Markdown, and link rewriting for instances already on Markdown
    files.py                attachment path resolution + path-traversal defence
specivo/cli/import_redmine.py        the command
specivo/models/import_id_map.py       the id-map ORM model
alembic/versions/0027_add_import_id_map.py
docker-compose.redmine.yml + tests/fixtures/redmine/seed.rb    the Redmine 7.0.1 test fixture
tests/unit/importers/          pure translation-function tests, no database
tests/services/test_import_*.py    loader tests against the test database, with hand-built IR
tests/integration/importers/        end-to-end import of the seeded Redmine fixture
```

## The IR seam, and how to add a new source

`specivo/importers/core/ir.py` is the boundary the whole design turns on. An
adapter reads its own system and emits these dataclasses; a loader consumes
them and never learns where they came from. Two rules make that hold:

1. **Every IR object carries a `source_ref`** — the source's own primary key,
   stringified. It is the only way IR objects reference each other
   (`project_ref`, `author_ref`, `parent_ref`, ...), and it is the key
   `ImportIdMap` stores mappings under. A loader never sees a raw source id
   typed as if it were already a Specivo one.
2. **Field names describe Specivo concepts, not source concepts.** Where the
   two disagree, the adapter does the translation before the IR is built —
   Redmine's binary `is_closed` becomes the four-way `IRStatus.category` in
   `extract.py`, not in a loader that would then need Redmine's vocabulary to
   make sense of it.

`EntityType` (also in `ir.py`) is the vocabulary shared between every adapter
and every loader for `import_id_map.entity_type`. Values are stable identifiers
— renaming one would orphan every mapping already written for it — and cover:

```
system   role       tracker    status      priority   activity
user     group      project    version     category   custom_field
issue    journal    relation   watcher     attachment
wiki_page  wiki_version  time_entry  membership
```

Streams are flat rather than nested — issues, journals, relations, watchers
and attachments are separate extraction streams, so each import phase can run
in its own transaction without holding a whole project in memory. Wiki
versions are the one exception: a page's history is small, and a page is
always loaded together with every revision it has.

### Adding a second source

1. Implement `SourceAdapter` (`specivo/importers/core/source.py`) — one
   `extract_*` async iterator per IR stream, plus `connect`, `close`, and
   `resolve_attachment_path`. Extraction methods should page on the source's
   own primary key rather than materialize a whole table; see `redmine/db.py`'s
   `stream()` for the pattern.
2. Implement `ContentConverter` (`specivo/importers/core/converter.py`) for the
   source's markup. `RedmineTextileConverter` is table stakes for what one
   looks like: never raise, and fall back to preserving the original text
   verbatim (wrapped so it still renders) rather than losing content on
   anything unconvertible.
3. Register the adapter in `SourceAdapterRegistry` (`core/source.py`) under a
   `source_system` name.
4. Nothing in `core/` or `load/` should need to change. If it does, that is a
   sign a loader learned something Redmine-specific it should not have.

## The phase pipeline

`ImportPhase` (`specivo/importers/core/pipeline.py`) is a `StrEnum` whose
*declaration* order is execution order — `PHASE_ORDER` is derived from it, so
adding a phase is a matter of inserting it in the right place. A phase with no
registered handler is skipped, which is what lets a source with no wiki simply
not register the wiki phases.

| # | Phase | Why it runs where it does |
|---|---|---|
| 1 | `BOOTSTRAP` | Creates the service account that owns rows with no resolvable author. Everything after this can rely on it existing. |
| 2 | `LOOKUPS` | Statuses, trackers, priorities, activities, roles — instance-wide, and every project and issue points at them. |
| 3 | `USERS` | Users, then group membership (recorded, not yet applied — Specivo cannot hold roles on a group). |
| 4 | `PROJECTS` | Parents before children (the adapter guarantees the order); resolves `--project` scope. |
| 5 | `PROJECT_LOOKUPS` | Versions and categories, which belong to a project that now exists. |
| 6 | `MEMBERSHIPS` | Needs both users (3) and projects (5); flattens group grants into per-user ones here. |
| 7 | `CUSTOM_FIELD_SCHEMAS` | Needs trackers (2) and projects (4) to scope a schema to. |
| 8 | `ISSUES` | Needs trackers, statuses, priorities, categories, versions, and other issues (for parents) — the latest possible point before journals need issues to exist. |
| 9 | `WATCHERS` | Issue watchers — needs issues (8) and users (3). |
| 10 | `CUSTOM_FIELD_VALUES` | Resolves user/version-valued custom fields — deferred because the referenced entity might not have existed yet when its issue was created. |
| 11 | `JOURNALS` | History — needs every issue in the *project* to exist, since a journal can reference any of them. |
| 12 | `RELATIONS` | Needs every issue in the *run*, not just one project — a relation can cross projects, and the far end may belong to a project imported later. |
| 13 | `ISSUE_ATTACHMENTS` | Needs issues (8). |
| 14 | `ISSUE_REF_REWRITE` | Rewrites `#123` to `KEY-N` and restores locked/closed version status — both need every issue in the run to exist first. |
| 15 | `WIKI_PAGES` | Independent of issues; runs after them mainly so issue-side failures are known before the (often larger) wiki history is replayed. |
| 16 | `WIKI_ATTACHMENTS` | Needs wiki pages (15). |
| 17 | `TIME_ENTRIES` | Needs projects and, optionally, issues (8) to attach to. |
| 18 | `WIKI_LINK_GRAPH` | Needs every wiki page in the run — a link graph is meaningless until then. |
| 19 | `SEARCH_BACKFILL` | Runs last, over everything the id map recorded, so nothing is indexed twice and nothing not yet imported is indexed at all. |

`registry.py` is the one file that wires loader functions onto these phases —
`register_all(pipeline)` — and is deliberately the only place that says what a
full Redmine import consists of. The pipeline itself
(`ImportPipeline.register` / `.run`) knows nothing about Redmine, issues, or
wikis; it only knows phases, handlers, and transactions.

## The id map and idempotency

`ImportIdMap` (`specivo/importers/core/id_map.py`) wraps the `import_id_map`
table. Before creating anything, a loader asks `id_map.get(session,
entity_type, source_ref)`; if it returns a Specivo id, the loader records a
`skipped` count and moves on. After creating a row, the loader calls
`id_map.put(...)` in the *same* transaction as the row itself — that pairing
is what makes a resumed run trustworthy: a mapping can never exist for a row
that was rolled back, because they commit together or not at all.

Lookups are cached in memory per `ImportIdMap` instance (one instance per
run, shared across the per-phase sessions the pipeline hands out) so a phase
resolving thousands of references issues at most one query per distinct
source id. `preload(session, entity_type)` loads every mapping of one type up
front — worth it for a phase that repeatedly resolves a small, closed set of
references (statuses, trackers, users). `get_many` batches a page of
references into one query instead of one query per row.

**Not every entity type is checked against the id map before writing.**
Memberships are the exception: `load_memberships` calls
`ProjectService.add_member` unconditionally for every membership row the
adapter yields, rather than checking whether this membership was already
imported. That is safe because `add_member` itself merges roles into any
existing membership rather than creating a duplicate — the idempotency comes
from the service's own behavior, not from a mapping check. Every other entity
type does check the map first.

`require(session, entity_type, source_ref)` is the same lookup as `get`, but
raises `MissingMappingError` instead of returning `None` — for references that
must resolve, where a missing mapping means a phase ran out of order rather
than that the source data was sparse. Optional references use `get` and
handle `None` themselves (falling back to the import service account, leaving
a field unset, or warning and skipping, depending on the field).

## The transaction model

A normal run gives each phase its own session
(`ImportPipeline._phase_session`), committed when the phase's handlers finish
and rolled back if any of them raise. `--resume` relies on this: a failure in
phase 14 leaves phases 1–13 committed, and a resumed run re-derives what is
left to do purely from what `import_id_map` and the target tables already
contain — there is no separate resume bookkeeping.

A dry run (`options.dry_run`) shares **one** session across every phase and
rolls it back unconditionally when the run ends, success or failure. This
exercises the real write path — the same service calls, the same validation —
while guaranteeing nothing persists. It is also why a dry run's report is not
a perfect preview of a real run: identifiers assigned to avoid a collision (a
suffixed login, a suffixed project key) reflect collisions with rows the dry
run itself created, which will not exist when the real run starts fresh.

## Extension points

| Protocol / class | Purpose | Where |
|---|---|---|
| `SourceAdapter` | Read one source system, emit IR | `core/source.py` |
| `ContentConverter` | One source's markup -> Specivo Markdown | `core/converter.py` |
| `ProgressReporter` | Phase/item/warning events — `CliProgressReporter` logs; a future admin UI could push the same events over a WebSocket | `core/progress.py` |
| `SourceAdapterRegistry` | Name -> adapter factory | `core/source.py` |

## Field-by-field mapping (Redmine)

Verify any of this against `specivo/importers/redmine/extract.py` before
relying on it in code — this table is a summary, not the source of truth.

| Redmine | Specivo | Rule |
|---|---|---|
| `trackers.fields_bits` | `trackers.disabled_core_fields` | Bitmask decoded against `REDMINE_CORE_FIELDS` (order matters, copied from Redmine's `Tracker::CORE_FIELDS`); `parent_issue_id` renamed to `parent_id`. |
| `issue_statuses.is_closed` + name | `issue_statuses.category` | `is_closed` -> `closed`. Otherwise: name matched against a small backlog/done hint list, else `active`. An operator `--status-category-map` override (matched case-insensitively on name) always wins. |
| `users.status` (0–3) | `users.status` | `1` -> `active`, `2` -> `pending_verification`, `3` -> `locked`. Anything else (including `0`, anonymous) -> `locked` — refusing sign-in to an unrecognized state is the safe direction to be wrong in. |
| `*.created_on` / `*.updated_on` (naive) | `*.created_at` / `*.updated_at` (tz-aware) | Stamped UTC as-is — Rails writes UTC into a timestamp-without-timezone column, so only the marker is missing. |
| `issue_relations.relation_type` (9 spellings) | `issue_relations.relation_type` (5 canonical) | `relates`, `duplicates`, `blocks`, `precedes`, `copied_to` are kept as-is; `duplicated`, `blocked`, `follows`, `copied_from` become their canonical form with `issue_from`/`issue_to` swapped. An unrecognized type is dropped and counted, not stored as something that would mean the wrong thing. |
| `custom_fields.field_format` | JSON Schema fragment | `list`/`enumeration` -> string (with `enum` if choices are known); `user`/`version` -> integer (a Specivo id, resolved after import); `int`/`float`/`bool`/`date`/`link`/`text`/`string` -> the obvious JSON Schema type; anything unrecognized -> string, which stores the value faithfully even when its shape is not understood. `multiple` wraps the result in an array. `min_length`/`max_length` become `minLength`/`maxLength` on string-shaped formats. |
| `projects.identifier` | `projects.key` | Redmine has no per-project prefix; Specivo's is derived from the identifier (uppercased, non-alphanumerics stripped, forced to start with a letter, suffixed on collision) unless `--project-key-map` supplies one. Every derived key is reported — this is the mapping operators most often want to correct. |
| `wiki_content_versions.data` + `.compression` | `wiki_contents.text` | Bytes, gzip-decompressed when `compression == "gzip"`, decoded UTF-8 with `errors="replace"` — a garbled revision is kept rather than losing the page's history over one bad byte. |
| `versions.status` | `versions.status`, applied late | Created `open` regardless of the source's recorded status (Specivo refuses an issue on a locked/closed version, and the source is full of issues sitting on exactly those); the real status is set in `ISSUE_REF_REWRITE` once every issue that might target the version exists. |
| Redmine module names | `projects.modules` | Only `issue_tracking`, `wiki` and `time_tracking` have a Specivo equivalent (`MODULE_MAP` in `extract.py`); everything else (repository, boards, news, documents, calendar, gantt) is dropped and counted per project. |
| `time_entries.hours` (float) | `time_entries.hours` (`Numeric(10,2)`) | Converted through `Decimal(str(...))`, not `Decimal(float)`, so `7.5` does not become `7.499999999999999`; rounded to two places, and a rounding that discards more than float noise (roughly `1e-9`) is reported as real precision lost. An entry that rounds to zero hours is skipped rather than stored as a phantom entry. |

### What is not carried across, and why

- **Passwords.** Redmine hashes with salted SHA1; Specivo uses bcrypt. Nothing
  is portable, so every imported account gets a random unusable hash and its
  login is listed in the report.
- **Groups, as groups.** Specivo cannot hold a project role on a group, so a
  group's grant is expanded into an identical grant for each of its current
  members. Access matches the source exactly; the fact that it came from a
  group does not survive.
- **Role permissions.** Redmine's permission vocabulary is serialized Ruby
  YAML naming Redmine's own permission set, which does not correspond to
  Specivo's one to one. Translating it would silently widen or narrow access,
  so roles are matched by name and an unmatched one is created with no
  permissions, listed for an administrator to fill in by hand.
- **Custom fields on anything but issues.** Specivo's metadata schemas only
  target issues today; fields on users, projects, versions, groups and time
  entries are counted (`count_non_issue_custom_fields`) and dropped.
- **Per-project priority and activity overrides.** Specivo's priorities and
  activities are global; a Redmine row with a `project_id` is an override the
  extractor filters out entirely (`count_project_enumerations` counts what was
  dropped).
- **Attachments on anything but an issue or a wiki page.** Files on a forum
  message, a document, a version or a project belong to features Specivo does
  not have (`count_unsupported_attachments`).
- **Redmine's own attachment digest.** Recomputed on copy instead — the point
  of a hash here is to describe the bytes that actually arrived, and
  Redmine's algorithm has changed across versions.

## The fixture

Development runs against a real Redmine rather than a mock of one, because the
things that break are Redmine's own conventions: its storage layout, its
bookkeeping, what its models actually write. See ADR-0006 for what this
tier of testing has already caught.

```
make redmine-fixture-up PROFILE=pg      # or PROFILE=mysql
make redmine-fixture-seed PROFILE=pg
make redmine-fixture-down PROFILE=pg
```

Redmine 7.0.1, seeded through Redmine's own models so every row is written the
way a real instance writes it. Both profiles exist because the importer
supports both databases Redmine runs on and the schemas are only identical in
theory — the MySQL one has already caught a defect the PostgreSQL one hid (see
ADR-0006).

The seed covers the cases the importer has to survive: a subproject and an
archived project, accounts in every state, a group membership, a parent issue
with subtasks, history of each shape, a private note, relations including one
across projects, a custom field of every format, a wiki page with three
revisions and a rename, attachments including a binary and a type Specivo
would not accept from an upload today, non-Latin text, and time entries with
more precision than two decimal places. It stores Textile, so markup
conversion is exercised; Redmine 7 itself defaults to CommonMark.

## Tests

```
make test           # unit and service tests; the fixture is not needed
make test-serial    # includes the end-to-end import, if the fixture is up
```

Three tiers, in `pytest.mark` terms:

- `tests/unit/importers/` — pure functions in `extract.py`, `textile_convert.py`,
  `reference_rewrite.py` and the pipeline's ordering logic, tested against
  literal dicts and hand-built IR. No database, no Redmine.
- `tests/services/test_import_*.py` — one loader at a time, driven against the
  test database with hand-built IR through a `FakeAdapter`, checking what each
  loader writes.
- `tests/integration/importers/` — imports the seeded Redmine fixture end to
  end and checks the result the way an operator would. Marked `redmine` (skips
  with instructions when the fixture is not running) and `serial` (an import
  writes instance-wide rows — roles, statuses, activities, the import account
  — that other tests insert too; running both under `pytest-xdist` deadlocks
  on each other's uncommitted index entries).
