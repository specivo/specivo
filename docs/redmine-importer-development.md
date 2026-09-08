# Working on the Redmine importer

Operator-facing instructions live in the user guide, under
[Migrating from Redmine](guide/install/migrating-from-redmine.md). This page is
for changing the importer itself.

## Layout

```
specivo/importers/
  core/    intermediate representation, protocols, phase pipeline, id map
  load/    loaders that turn IR into Specivo rows; they know no source system
  redmine/ the Redmine adapter: schema, extraction, markup, file layout
specivo/cli/import_redmine.py   the command
```

The intermediate representation is the seam. An adapter reads its own system
and emits IR; loaders consume IR and never learn where it came from. Adding
another source — RT, Jira — means writing an adapter package that implements
`SourceAdapter` and a converter for that system's markup. Nothing in `core` or
`load` should need to change.

## The fixture

Development runs against a real Redmine rather than a mock of one, because the
things that break are Redmine's own conventions: its storage layout, its
bookkeeping, what its models actually write.

```
make redmine-fixture-up PROFILE=pg      # or PROFILE=mysql
make redmine-fixture-seed PROFILE=pg
make redmine-fixture-down PROFILE=pg
```

Redmine 7.0.1, seeded through Redmine's own models so every row is written the
way a real instance writes it. Both profiles exist because the importer
supports both databases Redmine runs on and the schemas are only identical in
theory — the MySQL one has already caught a defect the PostgreSQL one hid.

The seed covers the cases the importer has to survive: a subproject and an
archived project, accounts in every state, a group membership, a parent issue
with subtasks, history of each shape, a private note, relations including one
across projects, a custom field of every format, a wiki page with three
revisions and a rename, attachments including a binary and a type Specivo would
not accept from an upload today, non-Latin text, and time entries with more
precision than two decimal places. It stores Textile, so markup conversion is
exercised; Redmine 7 itself defaults to CommonMark.

## Tests

```
make test           # unit and service tests; the fixture is not needed
make test-serial    # includes the end-to-end import, if the fixture is up
```

The end-to-end tests are marked `redmine`, so they skip with instructions when
the fixture is not running, and `serial`, because an import writes
instance-wide rows that other tests insert too and running both in parallel
deadlocks.

Unit tests cover the translation decisions against literal rows; service tests
drive the loaders against the test database with hand-built IR; the end-to-end
tests import the fixture and check the result the way an operator would.
