# The import framework

Core carries one import path: `python -m specivo.cli.import_data --source bundle`.
An importer for another tracker (Redmine, Jira) is a separate tool that reads that
tracker and writes an import bundle, specified in
[`docs/import-bundle/v1.md`](../import-bundle/v1.md). This page is for changing
the code that loads a bundle: its architecture, the phase pipeline, the identifier
map and how the tests are laid out.

Three decisions shape everything below. The write path goes through Specivo's
own services, so sequence numbers, nested sets and auto-watching stay correct
as those services change. The identifier map lives in a database table, written
in the same transaction as the rows it describes, so a rolled-back write never
leaves a stale entry. Each phase commits on its own, so a failure late in a
large import keeps what the earlier phases loaded and `--resume` continues from
there.

## Package layout

```
specivo/importers/
  core/
    ir.py            intermediate representation dataclasses, EntityType vocabulary
    source.py         SourceAdapter protocol, BaseSourceAdapter, the source registry (bundle only)
    cli_args.py        key=value / comma-list parsers shared by the command and adapters
    converter.py      ContentConverter protocol, ConversionContext, PassthroughConverter
    pipeline.py        ImportPhase (18 phases), ImportPipeline, ImportOptions, ImportSummary, PhaseContext
    id_map.py          ImportIdMap over the import_id_map table
    placeholders.py     the {{issue:ref}} placeholder syntax a bundle's text uses
    links_file.py        the SQLite links file that carries unresolved placeholders, relations and parents to a later bundle
    backdate.py         restores original created_at/updated_at after a service creates a row
    progress.py         ProgressReporter protocol, CliProgressReporter, NullProgressReporter
  bundle/
    adapter.py          BundleSourceAdapter — the one source; reads and validates a bundle
    scan.py, codec.py    validation, and records to IR
    schemas/v1/          the JSON Schemas the format is defined by
  load/
    registry.py         wires loaders onto phases — the one file that says what a full import consists of
    lookup_loader.py     statuses, trackers, priorities, activities, roles
    user_loader.py        users, the import service account, user groups
    project_loader.py     projects, versions, categories, memberships, custom-field schemas
    issue_loader.py        issues, journals, watchers
    issue_links.py         relations and deferred parents, also across bundles
    wiki_loader.py          wiki pages, revisions, watchers, redirects
    attachment_loader.py    file copy for issues and wiki pages
    time_entry_loader.py     logged time
    placeholder_rewrite.py    placeholder -> link expansion, once every record exists
    pending_links.py          placeholders waiting for a later bundle; opens the links file for a phase
    search_index.py           wiki link graph rebuild + search backfill, run once at the end
specivo/cli/import_data.py           the command (--source bundle)
specivo/models/import_id_map.py       the id-map ORM model
alembic/versions/0027_add_import_id_map.py
tests/unit/importers/          bundle format, codec, validation and pipeline ordering, no database
tests/services/test_import_*.py    loader tests against the test database, with hand-built IR
tests/integration/importers/        end-to-end loads of the example bundle
tests/fixtures/import_bundle/       the example bundle (full/) and helpers that write small ones
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
   two disagree, the importer does the translation before the IR is built —
   a source's binary "is closed" flag becomes the four-way `IRStatus.category`
   in the bundle, not in a loader that would then need that source's
   vocabulary to make sense of it.

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

### Importing from another tracker

Not a change to this repository. Write a tool that reads the tracker and
writes a bundle (the migration SDK has a writer and a `specivo-bundle validate`
command), and load it with `--source bundle`. Nothing in `core/` or `load/`
should need to change for it. If it does, that is a sign a loader learned
something about one source it should not have.

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
| 3 | `USERS` | Users, then the groups they belong to — in that order, since a group can only hold members the run has created. |
| 4 | `PROJECTS` | Parents before children (the adapter guarantees the order); resolves `--project` scope. |
| 5 | `PROJECT_LOOKUPS` | Versions and categories, which belong to a project that now exists. |
| 6 | `MEMBERSHIPS` | Needs users and groups (3) and projects (5); a group's grant is one membership row held by the group. |
| 7 | `CUSTOM_FIELD_SCHEMAS` | Needs trackers (2) and projects (4) to scope a schema to. |
| 8 | `ISSUES` | Needs trackers, statuses, priorities, categories, versions, and other issues (for parents) — the latest possible point before journals need issues to exist. A parent this run already created is set as the child is created; any other parent waits for `RELATIONS`. |
| 9 | `WATCHERS` | Issue watchers — needs issues (8) and users (3). |
| 10 | `JOURNALS` | History — needs every issue in the *project* to exist, since a journal can reference any of them. |
| 11 | `RELATIONS` | Needs every issue in the *run*, not just one project — a relation or a parent can cross projects, and the far end may belong to a project imported later, or to another bundle. Sets the parents `ISSUES` deferred and those earlier runs left in the links file, then makes the run's relations and the waiting ones; parents first, so a relation is checked against the final hierarchy. |
| 12 | `ISSUE_ATTACHMENTS` | Needs issues (8). |
| 13 | `VERSION_STATUSES` | Restores locked/closed version status — a locked version cannot take an issue, so it needs every issue in the run to exist first. |
| 14 | `WIKI_PAGES` | Independent of issues; runs after them mainly so issue-side failures are known before the (often larger) wiki history is replayed. |
| 15 | `WIKI_ATTACHMENTS` | Needs wiki pages (14). |
| 16 | `TIME_ENTRIES` | Needs projects and, optionally, issues (8) to attach to. |
| 17 | `WIKI_LINK_GRAPH` | Expands `{{issue:ref}}` placeholders in imported text once every record exists, then rebuilds the link graph, which needs every wiki page in the run. |
| 18 | `SEARCH_BACKFILL` | Runs last, over everything the id map recorded, so nothing is indexed twice and nothing not yet imported is indexed at all. |

`registry.py` is the one file that wires loader functions onto these phases —
`register_all(pipeline)` — and is deliberately the only place that says what a
full import consists of. The pipeline itself
(`ImportPipeline.register` / `.run`) knows nothing about any source, issues, or
wikis; it only knows phases, handlers, and transactions.

## Issue fields outside IssueCreate

`issue_loader.py` creates every issue through `IssueService.create`, so an
imported issue goes through the same checks as one made in the UI. Two kinds
of value are written after it, on the row the service returned: the source's
timestamps (`backdate`), and `remaining_estimate`, which `IssueCreate` does not
take because nothing in Specivo edits it yet. It is the time left as the source
tracked it (Jira's remaining estimate, for example) and is stored as given;
`estimated_hours` stays the estimate Specivo shows, sums and journals. The
remaining estimate's own history is not imported: a source like Jira lowers it
on every worklog, and Specivo keeps no history for that column.

## History values that name a record

Specivo's history keeps a changed attribute's value as text: a Specivo id for
an attribute that names a record (`status_id`, `fixed_version_id`, …), which
the issue page turns into a name (`specivo/web/activity_lookups.py`). A bundle
cannot know those ids, so such a change carries the records' refs (`old_ref`,
`new_ref`) and, in `old_value` / `new_value`, the source's text for them.
`issue_loader.py` resolves the refs through the id map when it creates the
entry (`REFERENCE_ATTRIBUTES` in `core/ir.py` says which attributes, and of
what entity type): a Specivo id, or for `parent_id` the parent's display key.
A ref the map does not hold is never stored, since the page would read the
source's id as one of its own; the source's text is stored instead, quoted if
it is only digits, and the phase warns once with the count. History refs are
resolved once, not kept waiting in the links file: a parent from a bundle
loaded later shows as its text.

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

## Links between bundles

A source exported in parts links issues across them: a placeholder, a parent,
a relation. Validation lets these three name an issue outside the bundle (an
`external_ref` warning); every other reference must resolve inside it. On
load, each is looked up in the identifier map of the scope, which holds every
earlier import of the source. What does not resolve yet is kept in the links
file (`core/links_file.py`), next to the bundles, and every later load of the
scope (and `admin import-map resolve-pending`) retries it:

- **Texts** are rewritten in `WIKI_LINK_GRAPH` (`placeholder_rewrite.py`,
  `pending_links.py`); an edited text is never overwritten (hash check).
- **Parents and relations** are made in `RELATIONS` (`issue_links.py`), with
  the same checks as anywhere else (`NestedSetService.validate_parent` plus a
  depth check that counts the child's own sub-tasks, `RelationService.create`);
  a refusal is a warning and drops the waiting row. A relation is keyed by its
  `source_ref` in the id map, so one carried by two bundles is made once. A
  waiting parent is applied only to an issue with no parent. The deferred
  parents of this run are found again from the bundle and
  `ImportIdMap.written_by_run`, not from memory, so a resumed run (same run id)
  finds them too.

Each phase that uses the file opens it in one SQLite transaction and commits it
from an `at_phase_end` hook, after the phase's database transaction has
committed; so the file never describes rows that were rolled back. Waiting
parents and relations are idempotent (a mapped relation or an already-set
parent is simply dropped), so a file that lags behind the database heals on
the next load. Every row is HMAC-signed with a key derived from `SECRET_KEY`;
the file is refused if it is a symbolic link or of another format version
(currently 2) or scope, and deleted when nothing waits.

## The transaction model

A normal run gives each phase its own session
(`ImportPipeline._phase_session`), committed when the phase's handlers finish
and rolled back if any of them raise. `--resume` relies on this: a failure in
a late phase leaves the earlier ones committed, and a resumed run re-derives what is
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

## Tests

```
make test           # everything except serial and e2e
make test-serial    # includes the end-to-end bundle loads
```

- `tests/unit/importers/` — the bundle format, codec and validation, the
  command's argument handling and the pipeline's ordering, against literal
  dicts and hand-built IR. No database.
- `tests/services/test_import_*.py` — one loader at a time, against the test
  database, with hand-built IR served by a `FakeAdapter`.
- `tests/integration/importers/` — loads `tests/fixtures/import_bundle/full`
  end to end and checks the result the way an operator would. Marked `serial`:
  a load writes instance-wide rows (roles, statuses, activities, the import
  account) that other tests insert too, and running both under `pytest-xdist`
  deadlocks on each other's uncommitted index entries.
