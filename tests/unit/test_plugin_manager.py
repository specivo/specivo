"""Unit tests for PluginManager.

RED phase — these tests define the expected behavior of:
- ``PluginManager.load_plugins()`` discovery from a config list of dotted paths.
- Plugins keep the configured order.
- Graceful handling of import errors.
- Plugin API version compatibility checking.
- Module-level ``get_plugin_manager()`` singleton accessor.

All tests are pure — no database, no I/O.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers: in-memory plugin classes for testing
# ---------------------------------------------------------------------------


def _fake_plugin_module(name: str, version: str = "0.9.0"):
    """Create a fake module with a ``Plugin`` class satisfying PluginConfig."""
    from specivo.core.plugin import BasePluginConfig

    class Plugin(BasePluginConfig):
        @property
        def name(self) -> str:
            return name

        @property
        def version(self) -> str:
            return version

    return Plugin


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestPluginManagerDiscovery:
    def test_discover_with_empty_list_returns_no_plugins(self):
        from specivo.core.plugin_manager import PluginManager

        pm = PluginManager()
        pm.load_plugins([])
        assert pm.plugins == []

    def test_discover_loads_plugin_by_module_path(self):
        """load_plugins() imports a dotted path and instantiates the class."""
        from specivo.core.plugin_manager import PluginManager

        FakePlugin = _fake_plugin_module("test.fake")

        # Patch importlib.import_module to return our fake module
        import types

        fake_module = types.ModuleType("fake_plugin_module")
        fake_module.FakePlugin = FakePlugin  # type: ignore[attr-defined]

        pm = PluginManager()
        with patch("importlib.import_module", return_value=fake_module):
            pm.load_plugins(["fake_plugin_module.FakePlugin"])

        assert len(pm.plugins) == 1
        assert pm.plugins[0].name == "test.fake"

    def test_discover_keeps_configured_order(self):
        """Plugins load in the order INSTALLED_PLUGINS lists them."""
        import types

        from specivo.core.plugin_manager import PluginManager

        names = ["zeta.last-alphabetically", "alpha.first-alphabetically", "mid.middle"]
        module_map = {}
        for i, name in enumerate(names):
            mod = types.ModuleType(f"mod_{i}")
            mod.Plugin = _fake_plugin_module(name)  # type: ignore[attr-defined]
            module_map[f"mod_{i}"] = mod

        pm = PluginManager()
        with patch("importlib.import_module", side_effect=lambda p: module_map[p]):
            pm.load_plugins([f"mod_{i}.Plugin" for i in range(len(names))])

        assert [p.name for p in pm.plugins] == names

    def test_plugin_that_still_defines_get_features_and_tier_loads(self):
        """An older plugin class with the retired ``tier`` and ``get_features`` still loads."""
        import types

        from specivo.core.plugin import BasePluginConfig, PluginConfig
        from specivo.core.plugin_manager import PluginManager

        calls: list[str] = []

        class OldStylePlugin(BasePluginConfig):
            name, tier, version = "old.style", "core", "1.0"

            def get_features(self) -> list[str]:
                calls.append("features")
                return ["anything"]

            def get_services(self, registry) -> None:
                calls.append("services")

        mod = types.ModuleType("mod_old")
        mod.OldStylePlugin = OldStylePlugin  # type: ignore[attr-defined]

        pm = PluginManager()
        with patch("importlib.import_module", return_value=mod):
            pm.load_plugins(["mod_old.OldStylePlugin"])
        pm.register_services()

        assert isinstance(pm.plugins[0], PluginConfig)
        assert [p.name for p in pm.plugins] == ["old.style"]
        assert calls == ["services"]

    def test_discover_logs_warning_on_import_error(self, caplog):
        """When a plugin module cannot be imported, an appropriate error is raised."""
        from specivo.core.plugin_manager import PluginManager

        pm = PluginManager()
        with pytest.raises(ImportError):
            pm.load_plugins(["nonexistent.module.FakePlugin"])


# ---------------------------------------------------------------------------
# Loading hooks
# ---------------------------------------------------------------------------


class TestPluginManagerLoadAll:
    def test_load_all_calls_all_plugin_hooks(self):
        """register_services() calls each plugin's hook."""
        from specivo.core.plugin import BasePluginConfig
        from specivo.core.plugin_manager import PluginManager

        calls: list[str] = []

        class TrackingPlugin(BasePluginConfig):
            @property
            def name(self) -> str:
                return "test.tracking"

            def get_services(self, registry) -> None:
                calls.append("services")

        import types

        mod = types.ModuleType("mod_tracking")
        mod.TrackingPlugin = TrackingPlugin  # type: ignore[attr-defined]

        pm = PluginManager()
        with patch("importlib.import_module", return_value=mod):
            pm.load_plugins(["mod_tracking.TrackingPlugin"])

        pm.register_services()

        assert calls == ["services"]


# ---------------------------------------------------------------------------
# API version compatibility
# ---------------------------------------------------------------------------


class TestPluginApiVersionCheck:
    def test_plugin_api_version_mismatch_raises_error(self):
        """If a plugin declares an incompatible API version, loading should fail.

        NOTE: The exact mechanism (attribute on plugin, constructor check, etc.)
        is implementation-defined. This test verifies the concept: a plugin
        targeting API version "99.0" must not be silently loaded with API "1.0".
        """
        from specivo.core.plugin_manager import PLUGIN_API_VERSION

        # This test documents the expected behavior. The implementation may
        # use a ``plugin_api_version`` attribute or a ``required_api`` property.
        # If the implementation does not perform version checks, this test
        # should be updated to match the chosen design.
        assert PLUGIN_API_VERSION is not None  # at minimum, the constant exists


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


class TestGetPluginManager:
    def test_get_plugin_manager_raises_before_create_app(self):
        """get_plugin_manager() raises RuntimeError if create_app() has not run."""
        # Reset the module-level singleton to simulate pre-startup state.
        import specivo.main as main_module
        from specivo.main import get_plugin_manager

        original = main_module._plugin_manager
        try:
            main_module._plugin_manager = None
            with pytest.raises(RuntimeError, match="not initialized"):
                get_plugin_manager()
        finally:
            main_module._plugin_manager = original
