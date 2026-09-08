"""Redmine source adapter.

Reads a Redmine 7.x database and its attachment directory, and emits the
source-agnostic intermediate representation the loaders consume.
"""

from specivo.importers.core.source import registry
from specivo.importers.redmine.adapter import RedmineSourceAdapter

registry.register(RedmineSourceAdapter.source_system, RedmineSourceAdapter)

__all__ = ["RedmineSourceAdapter"]
