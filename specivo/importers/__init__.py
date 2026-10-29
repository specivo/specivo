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

There is one source, ``bundle``. An importer for another tracker is a separate
tool that reads that tracker and writes a bundle; nothing in ``core`` or
``load`` changes for it.
"""
