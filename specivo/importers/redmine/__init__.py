"""Redmine source adapter.

Reads a Redmine 7.x database and its attachment directory, and emits the
source-agnostic intermediate representation the loaders consume. Registered
as the built-in ``redmine`` source by
:func:`specivo.importers.core.source.build_source_registry`.
"""

from specivo.importers.redmine.adapter import RedmineSourceAdapter

__all__ = ["RedmineSourceAdapter"]
