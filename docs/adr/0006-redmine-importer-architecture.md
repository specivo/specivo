# ADR-0006: Redmine Importer Architecture

**Date:** 2026-09-08
**Status:** Accepted
**Deciders:** Boris

## Context

Specivo is self-hosted, and most teams evaluating it already run Redmine. A
migration path only pays off if it can carry over the things people actually
depend on — issue history, private notes, wiki revisions, logged time — not
just the current state of each record, and if it can be re-run safely when it
fails partway through a large instance.

Two things about Specivo's own architecture shaped how this had to be built
rather than left as an open design space:

- Creating an issue, a wiki page, a project or a version already goes through
  a service that does more than insert a row — per-project sequence numbers,
  nested sets, ltree paths, auto-watching, slug generation. An importer that
  bypassed those services would have to reimplement them, and would drift from
  the application's own behavior the next time one of them changed.
- Specivo has no notion of "this row came from an import." Rows created by the
  importer are ordinary rows, indistinguishable from ones a user created by
  hand, which is the point — but it means an interrupted or re-run import has
  nothing on the target side to recognize what it already did.

A second migration source (RT, Jira) is expected eventually, so the design
also had to decide how much of this would be Redmine-specific versus shared.

## Decision

### 1. Read the source database directly, not the REST API

The importer connects to Redmine's database with a read-only user and reads
its attachment directory read-only. Nothing is written to the source.

Redmine's REST API cannot see private notes or the accounts of deleted users,
and does not expose complete history in a form worth reconstructing from. It
is also far slower for an instance of any size — every issue, journal entry
and attachment would be a request. Reading the database directly gets a
complete, fast, single-pass extraction, at the cost of assuming the operator
has database and filesystem access to the source. That is a fair assumption
for somebody migrating their own tracker off Redmine, which is the only
audience this importer serves.

### 2. The intermediate representation is the seam

`specivo/importers/core/ir.py` defines a set of dataclasses — `IRProject`,
`IRIssue`, `IRWikiPage`, and so on — that describe Specivo concepts, not
Redmine ones. `specivo/importers/redmine/adapter.py` is the only module that
knows Redmine's schema; it reads Redmine and emits IR. Everything under
`specivo/importers/load/` consumes IR and has no idea where it came from.

Two rules keep that boundary real rather than aspirational:

- Every IR object carries a `source_ref` — the source's own primary key,
  stringified — which is the only way IR objects reference each other
  (`project_ref`, `author_ref`, and so on). A loader never sees a source
  system's numeric id typed as if it already were a Specivo one.
- Where the two systems disagree on a concept, the adapter resolves it before
  the IR is built. Redmine's binary `is_closed` becomes the four-way
  `IRStatus.category` in `extract.py`, not in a loader that would otherwise
  need to know Redmine's vocabulary to interpret it.

Adding RT or Jira later means writing a new adapter package and a
`ContentConverter` for that system's markup (`specivo/importers/core/
converter.py`). Nothing in `core` or `load` should have to change, because
nothing in either package imports anything Redmine-specific.

Extraction methods are async iterators, streamed and keyset-paginated on the
source's primary key (`specivo/importers/redmine/db.py:stream`), rather than
materializing a whole table — an instance with a hundred thousand issues has
to page through them without holding them all in memory at once, and an
`OFFSET`-based page would force the source database to rescan and discard a
growing prefix on every page.

### 3. Loaders write through Specivo's services, with two deliberate exceptions

`load_issues` calls `IssueService.create`; `load_wiki_pages` calls
`WikiService.create_page` for a page's first version; `load_projects` calls
`ProjectService.create`. Going through the same services the application uses
everywhere else means sequence numbers, nested sets, ltree paths and
auto-watch behavior are correct by construction, and stay correct the next
time those services change, without the importer having to track the change.

Two things do not go through a service, on purpose:

- **Journal history.** `JournalService` exists to diff a *live* issue against
  what it used to be — it inspects the current row and a proposed change and
  works out old and new values itself. A source system already recorded both
  values, in order; replaying that through a service built for the opposite
  direction would mean mutating an issue back and forth just to make the
  service produce the diff it was already given. `load_journals` builds
  `Journal` and `JournalDetail` rows directly instead — still ORM objects,
  still inside the phase's transaction — and numbers them in the order the
  source recorded them.
- **Wiki revisions after the first.** `WikiService.create_page` is built to
  take a page from one version to the next as somebody edits it. Seeding a
  history that already exists, with its own authors and dates per revision,
  is not that operation, so `load_wiki_pages` inserts `WikiContent` rows
  directly for versions 2 and up.

### 4. Idempotency lives in a table, not a state file

`import_id_map` (`specivo/models/import_id_map.py`,
`alembic/versions/0027_add_import_id_map.py`) records, for every entity a
loader creates, the source system, the source instance, the entity type, the
source's own id, and the Specivo table and row it became. Every loader checks
this map before creating anything and writes to it in the same transaction as
the row it describes.

A state file tracking "what has been imported" was rejected because a file
cannot be kept consistent with a database that might roll back. If the file
said a row existed and the transaction that was supposed to create it rolled
back, a resumed run would skip a row that was never there. Putting the mapping
in the same database, written in the same transaction, makes that
inconsistency impossible: either both the row and its mapping exist, or
neither does.

The mapping's uniqueness constraint — `source_system, source_instance,
entity_type, source_id` — deliberately **excludes** `import_run_id`. A resumed
run is given a fresh run id (or reuses the one the operator passes to
`--resume`), but it has to recognize rows an earlier, failed attempt already
wrote. If the run id were part of the key, a resume would either need the
exact same run id preserved and passed correctly, or it would create
duplicates. Excluding it means any earlier attempt at importing the same
source entity is found, whether or not the operator still has its run id
around; `import_run_id` is kept on the row for reporting only.

### 5. One transaction per phase; a dry run shares one and rolls it back

A normal run gives each phase (`specivo/importers/core/pipeline.py`) its own
session, committed when the phase finishes and rolled back if it raises. That
means a failure late in a large import — say, in wiki attachments — does not
discard the projects, issues, and journals that already committed
successfully, and `--resume` picks up from the first phase that has anything
left to do.

A dry run is different in kind, not degree: it shares **one** session across
every phase and rolls that session back unconditionally at the end, whether or
not every phase completed. That is what lets a dry run exercise the exact
write path a real run would use — the same service calls, the same
validation, the same conflicts with data already in Specivo — while
guaranteeing that nothing it did survives. The alternative, a separate
"preview" code path that predicts what an import would do without doing it,
was rejected because a predictor drifts from what the real path actually does
and would eventually tell an operator something the real run does not
deliver.

One consequence worth knowing: a dry run's report can name project keys,
status categories and warnings that a **second** dry run, or the real run,
produces slightly differently — anything whose uniqueness depends on rows the
first dry run would have created (a suffixed login on a name collision, for
instance) sees a different starting state once nothing from the dry run is
actually there. The report is a preview of the decisions the importer will
make, not a locked-in plan.

### 6. Search indexing and the wiki link graph run once, at the end

`IssueService.create`, `WikiService.create_page` and
`AttachmentService.upload_from_path` all accept `skip_search_index`, and
`WikiService.create_page` also accepts `skip_link_rebuild`. Every loader
passes them. Chunking and embedding every issue and wiki page as it is
created would make an import of any size crawl — embedding is the slowest
single operation in the write path — and dispatching a wiki link-graph task
per page would flood the Celery queue for no benefit, since the graph is
meaningless until every page in the run exists anyway.

`SEARCH_BACKFILL` and `WIKI_LINK_GRAPH` are the last two phases, running once
over everything the id map recorded. `WIKI_LINK_GRAPH` runs in-process rather
than through Celery: an import is typically run from a shell with no worker
necessarily listening, and a task dispatched into an empty queue would be
silently dropped. `SEARCH_BACKFILL` is skipped entirely on a dry run, since
none of the indexed rows would survive the rollback, and a failure inside it
is reported rather than allowed to fail the whole import — an import with an
unindexed tail is recoverable with the existing backfill command
(`specivo/cli/backfill_embeddings.py`); a failed import is not.

Full-text search needed no equivalent suppression: its `tsvector` columns are
maintained by database triggers that fire on the same inserts the importer
already does, so there was nothing to defer.

### 7. Two passes are deliberately deferred until every issue exists

- **Issue-reference rewriting.** Redmine writes a cross-issue reference as
  `#123`; Specivo writes it as `ACME-15`. That translation cannot happen
  while an issue is being created, because `#123` might point at an issue in
  a project the run has not reached yet — projects, and the issues inside
  them, are imported one at a time. `ISSUE_REF_REWRITE` runs after every
  selected project's issues exist, rewrites references it can resolve through
  the id map, and leaves anything it cannot resolve exactly as written —
  `specivo/importers/load/reference_rewrite.py` treats an unresolvable number
  as data (a version number, a quantity, a reference to something outside the
  run) rather than a broken link to fix.
- **User- and version-valued custom fields.** A custom field can hold a
  reference to a Redmine user or version. The issue that names one might be
  imported before the user or version it points at is, so the value is
  written verbatim as a source id when the issue is created and rewritten to
  a Specivo id afterwards, once every user and version in the run has been
  imported (`resolve_custom_field_references`).

Both follow from the same constraint: a loader can only resolve a reference
through the id map after the referenced entity has actually been imported,
and issues are imported project by project rather than in a single global
pass.

### 8. Where the two systems disagree, the import continues and reports

Nothing that can be worked around stops the import. A duplicate-named status
gets a second row unless `--merge-duplicate-statuses` says to reuse the
existing one; a project nested past Specivo's depth limit is skipped unless
`--flatten-excess-depth` says to re-parent it; a relation Specivo's validation
rejects is skipped with a warning rather than aborting the run. Each of these
is recorded on `ImportSummary` (`specivo/importers/core/pipeline.py`) and
appears in the report.

The one switch that inverts this is `--strict`
(`PhaseContext.warn`): everything that would otherwise become a warning
raises instead. That exists for testing the importer itself — a CI run that
wants to know the fixture produced *zero* surprises — not for an operator
migrating a real instance, whose Redmine will have accumulated exactly the
kind of small inconsistencies (a status with no clear category, a role Specivo
has no permission mapping for) that this decision exists to survive.

A dry run is what makes "continue and report" workable for an operator: the
report is the list of decisions to review, produced by a run that changed
nothing, before the same decisions are made for real.

### 9. Development and testing run against a real Redmine, not a mock

The importer is tested at three levels: unit tests exercise the pure
`extract.py` translation functions against literal row dicts; service-level
tests drive the loaders against the test database with hand-built IR; and an
end-to-end suite, marked `redmine` and `serial`, imports a Redmine 7.0.1
instance seeded through Redmine's own Rails models
(`tests/fixtures/redmine/seed.rb`, `docker-compose.redmine.yml`) and checks
the result the way an operator would.

The third tier exists because the failures worth designing around are not in
the translation logic, which unit tests already cover — they are in Redmine's
own bookkeeping, storage conventions, and the exact shapes its models produce,
none of which a hand-written mock would reproduce unless it already knew about
them. In practice this tier is what found the defects the design above now
accounts for:

- An issue that targeted a **locked version** could not be created — Specivo
  refuses to place an issue on a locked or closed version, and Redmine
  instances that have been running for years are full of exactly that. The
  first version of the importer created versions with their real status and
  lost the issue, silently taking its subtasks, journal, attachments and
  relations with it. Versions are now always created `open` and set to their
  recorded status only after every issue that might target one has been
  created (`restore_version_statuses` in `project_loader.py`).
- A **resumed run died on its first project**. The service account that owns
  rows with no resolvable author was recorded in the id map when it was
  *created*, but not when an already-existing one was *found* on a second
  run — so a resume found the account, used it, but the code path that would
  have looked it up by id map key had nothing there, and every phase after
  the first failed to find something that had never been recorded.
  `ensure_import_account` now records the mapping on both paths.
- A **dry run left copied files on disk**. The database rolls back; a file
  copied to Specivo's attachment storage during the same dry run does not.
  `AttachmentService.upload_from_path` gained a `copy_file` parameter, which
  the importer sets to `False` exactly when `options.dry_run` is true.
- **Connection pre-pinging is unusable against a MySQL source.**
  SQLAlchemy's pre-ping and the `aiomysql` driver disagree about the
  signature of `ping`, and pre-ping raises on the first checkout rather than
  silently doing nothing. `create_source_engine` recycles MySQL connections
  by age instead, comfortably inside MySQL's default idle timeout, which
  covers the same risk (a long import outliving an idle connection) without
  the broken path. PostgreSQL sources keep `pool_pre_ping=True`.

None of these would have surfaced against a hand-rolled fake source; each came
from a real instance behaving the way real instances do.

Both database backends Redmine supports are exercised (`PROFILE=pg|mysql`)
because the schema is only identical between them in theory — the MySQL
profile is what caught the pre-ping defect above, which the PostgreSQL
profile hid completely.

## Consequences

**Positive:**

- The IR boundary means a second source adapter is additive work, not a
  rewrite: `core` and `load` do not know Redmine exists.
- Going through Specivo's own services for the write path means the importer
  cannot drift from how the application creates these entities, because it
  uses the same code.
- Per-phase commits plus a table-backed id map make `--resume` genuinely safe
  rather than merely advertised: a resumed run re-derives exactly what is
  left to do from what is already in the database.
- A dry run exercising the real write path means its report is trustworthy —
  it reflects what the code will actually do, not a separate approximation of
  it.

**Negative:**

- The two write-path exceptions (journal history, wiki revisions after the
  first) are places a future change to `JournalService` or `WikiService`
  will not automatically reach the importer — they have to be checked by
  hand when either service changes shape.
- `import_id_map` is permanent, unbounded growth: every entity ever imported
  from every source stays mapped forever. Nothing currently prunes it. This
  is deliberate — a mapping is what makes a *second* import from the same
  source recognize what a first one already did, potentially long
  afterwards — but it is a table with no retention policy.
- Deferring reference rewriting and reference-valued custom fields to a pass
  over the whole run means those two phases re-scan every imported issue,
  rather than resolving each reference at creation time. For the instance
  sizes this importer targets that cost has not mattered in testing, but it
  is an O(issues) pass, twice, on top of the O(issues) creation pass.
- The MySQL and PostgreSQL fixture profiles roughly double the cost of
  changing anything in `redmine/db.py` or `redmine/adapter.py`: both have to
  be run before a schema-reading change can be trusted.

## Not Chosen

- **Reading Redmine through its REST API.** Rejected outright — private
  notes and deleted users' data are invisible to it, and it does not scale to
  an instance with real history.
- **A state file recording import progress.** Rejected because it cannot be
  kept transactionally consistent with a database that may roll back a phase;
  see Decision 4.
- **One transaction for the whole import.** Rejected because a single long
  transaction would hold locks and accumulate undo state for the entire
  run, and would discard every verified phase on a late failure. Per-phase
  transactions bound both costs and make `--resume` meaningful.
- **A separate dry-run predictor** that estimates what an import would do
  without running the real write path. Rejected because it would drift from
  the real path over time and eventually mislead the operator who trusted it;
  see Decision 5.
- **Translating Redmine's role permissions.** Considered and rejected:
  Redmine's permission vocabulary is its own, and an approximate mapping
  would quietly widen or narrow access in a way nobody asked for. Roles are
  matched by name and otherwise left for an administrator to configure by
  hand.
- **Failing the import on the first unmappable record.** Rejected in favor
  of continuing and reporting (Decision 8) — a hard failure partway through a
  multi-hour import over one status with an ambiguous category is a worse
  outcome than a warning the operator reviews afterwards, and `--strict`
  exists for the cases (mainly CI) where failing fast is actually wanted.
