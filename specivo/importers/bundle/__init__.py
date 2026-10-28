"""Import bundles: the file format importers write and core loads.

A bundle is a directory (or a ``.zip`` of one) holding a manifest, one
JSON-lines file per entity type and the attachment bytes, specified in
``docs/import-bundle/v1.md`` and described by the JSON Schemas in
``schemas/v1/``. It is the intermediate representation written to disk, so an
importer for any tracker can be a separate program that never links to core.

* ``adapter`` — :class:`BundleSourceAdapter`, the ``bundle`` import source.
* ``scan`` — validation and the index the adapter reads from.
* ``writer`` — IR to bundle, for tests and for exporting another source.
"""

from specivo.importers.bundle.adapter import BundleInvalidError, BundleSourceAdapter

__all__ = ["BundleInvalidError", "BundleSourceAdapter"]
