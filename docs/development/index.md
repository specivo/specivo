# Development guides

Notes for people changing Specivo, as opposed to running it.

Anything here is repo-only. The published user guide is `docs/guide/`, which is
built into the site at specivo.io; this directory is not part of that build and
is read on GitHub.

- [The import framework](import-framework.md) — how a bundle is loaded: the
  phase pipeline, the identifier map and the test layout.
- [Security scanning with Trivy](security-scanning.md) — what CI scans, how to
  run the same scans locally, and what to do when the release gate fails.

Related, elsewhere in the repo:

- `docs/adr/` — architecture decision records. Read these first when a design
  choice looks arbitrary; the reason is usually written down.
- `docs/guide/` — the published user and operator guide: English pages in
  `en/`, translations in a folder per language with the same file names (for
  example `zh-cn/`), and screenshots shared by all languages in `assets/`.
- `CLAUDE.md` — repo conventions, branch workflow and release process.
