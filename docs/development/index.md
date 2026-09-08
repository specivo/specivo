# Development guides

Notes for people changing Specivo, as opposed to running it.

Anything here is repo-only. The published user guide is `docs/guide/`, which is
built into the site at specivo.io; this directory is not part of that build and
is read on GitHub.

- [The Redmine importer](redmine-importer.md) — how the migration framework is
  put together, how to add another source, and how to run its Redmine fixture.

Related, elsewhere in the repo:

- `docs/adr/` — architecture decision records. Read these first when a design
  choice looks arbitrary; the reason is usually written down.
- `docs/guide/` — the published user and operator guide.
- `CLAUDE.md` — repo conventions, branch workflow and release process.
