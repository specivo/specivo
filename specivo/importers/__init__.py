"""Import framework — migrate external trackers into Specivo.

The package is split so that source-specific knowledge never leaks into the
write path:

* ``core`` — source-agnostic intermediate representation (IR), the
  ``SourceAdapter`` / ``ContentConverter`` / ``ProgressReporter`` protocols and
  the phase pipeline.
* ``load`` — loaders that turn IR objects into Specivo rows. They consume IR
  only and know nothing about any source system.
* ``bundle`` — the IR as a documented file format (``docs/import-bundle/v1.md``)
  and the adapter that reads it. Importers outside Specivo write bundles.
* ``redmine`` — the first source adapter: reads a Redmine database and its
  ``files/`` directory and emits IR.

Adding another source means writing an importer that produces a bundle, or a
new adapter package that implements ``SourceAdapter`` and emits the same IR.
No change to ``core`` or ``load`` is required either way.
"""
