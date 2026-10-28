"""The plugin contract: which hooks a plugin can implement."""

from __future__ import annotations

import pytest

from specivo.core.plugin import BasePluginConfig, PluginConfig
from specivo.core.plugin_manager import PluginManager

pytestmark = pytest.mark.unit


def test_plugin_has_no_import_source_hook():
    """Importers are tools that write bundles; a plugin cannot add an import source."""

    class Bare(BasePluginConfig):
        name, tier = "bare", "core"

    assert isinstance(Bare(), PluginConfig)
    assert not hasattr(PluginConfig, "get_import_sources")
    assert not hasattr(BasePluginConfig, "get_import_sources")
    assert not hasattr(PluginManager, "collect_import_sources")
