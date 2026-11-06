"""Unit tests for PluginConfig protocol and BasePluginConfig.

RED phase — these tests define the expected behavior of:
- ``PluginConfig``: a runtime-checkable Protocol requiring name and version.
- ``BasePluginConfig``: convenience base class with no-op defaults.
- ``PLUGIN_API_VERSION``: module-level constant for compatibility checks.

All tests are pure — no database, no I/O.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers: minimal concrete implementations for testing the protocol
# ---------------------------------------------------------------------------


def _make_minimal_plugin():
    """Create a minimal class satisfying PluginConfig at the protocol level."""
    from specivo.core.plugin import BasePluginConfig

    class MinimalPlugin(BasePluginConfig):
        @property
        def name(self) -> str:
            return "test.minimal"

    return MinimalPlugin()


# ---------------------------------------------------------------------------
# BasePluginConfig defaults
# ---------------------------------------------------------------------------


class TestBasePluginConfigDefaults:
    """BasePluginConfig provides sensible no-op defaults for every hook."""

    def test_default_version_is_zero(self):
        plugin = _make_minimal_plugin()
        assert plugin.version == "0.0.0"

    def test_get_models_returns_empty_list(self):
        plugin = _make_minimal_plugin()
        assert plugin.get_models() == []

    def test_get_routers_returns_empty_list(self):
        plugin = _make_minimal_plugin()
        assert plugin.get_routers(prefix="/api") == []

    def test_get_services_is_noop(self):
        """get_services() should accept a registry and do nothing."""
        plugin = _make_minimal_plugin()
        # Pass a dummy object; no-op should not raise.
        plugin.get_services(object())

    def test_get_celery_tasks_is_noop(self):
        plugin = _make_minimal_plugin()
        plugin.get_celery_tasks(object())  # should not raise

    def test_get_migration_path_returns_none(self):
        plugin = _make_minimal_plugin()
        assert plugin.get_migration_path() is None

    def test_get_template_dirs_returns_empty_list(self):
        plugin = _make_minimal_plugin()
        assert plugin.get_template_dirs() == []

    def test_on_startup_is_noop(self):
        plugin = _make_minimal_plugin()
        plugin.on_startup(object())  # should not raise


# ---------------------------------------------------------------------------
# PluginConfig protocol contract
# ---------------------------------------------------------------------------


class TestPluginConfigProtocol:
    """PluginConfig is a runtime-checkable Protocol."""

    def test_contract_has_no_tier_or_features(self):
        """Plugins load in the configured order; there is no tier or feature flag hook."""
        from specivo.core.plugin import BasePluginConfig, PluginConfig

        for attr in ("tier", "get_features"):
            assert not hasattr(PluginConfig, attr)
            assert not hasattr(BasePluginConfig, attr)

    def test_protocol_requires_name(self):
        """A class without ``name`` must not satisfy the protocol."""
        from specivo.core.plugin import PluginConfig

        class MissingName:
            @property
            def version(self) -> str:
                return "1.0.0"

        assert not isinstance(MissingName(), PluginConfig)

    def test_protocol_requires_version(self):
        """A class without ``version`` must not satisfy the protocol."""
        from specivo.core.plugin import PluginConfig

        class MissingVersion:
            @property
            def name(self) -> str:
                return "x"

        assert not isinstance(MissingVersion(), PluginConfig)

    def test_base_plugin_config_satisfies_protocol(self):
        """A BasePluginConfig subclass with a name satisfies PluginConfig."""
        from specivo.core.plugin import PluginConfig

        plugin = _make_minimal_plugin()
        assert isinstance(plugin, PluginConfig)


# ---------------------------------------------------------------------------
# Hook return types with concrete implementations
# ---------------------------------------------------------------------------


class TestPluginConfigHookReturnTypes:
    """Verify that hooks return the correct types when overridden."""

    def test_get_models_returns_list(self):
        from specivo.core.plugin import BasePluginConfig

        class WithModels(BasePluginConfig):
            @property
            def name(self) -> str:
                return "test.models"

            def get_models(self) -> list:
                return [object, int]  # stand-in for model classes

        plugin = WithModels()
        result = plugin.get_models()
        assert isinstance(result, list)
        assert len(result) == 2

    def test_get_routers_returns_list_of_tuples(self):
        from specivo.core.plugin import BasePluginConfig

        class WithRouters(BasePluginConfig):
            @property
            def name(self) -> str:
                return "test.routers"

            def get_routers(self, prefix: str) -> list:
                fake_router = object()
                return [(fake_router, {"prefix": f"{prefix}/api/v1", "tags": ["test"]})]

        plugin = WithRouters()
        result = plugin.get_routers("/sp")
        assert isinstance(result, list)
        assert len(result) == 1
        router, kwargs = result[0]
        assert "prefix" in kwargs
        assert kwargs["prefix"] == "/sp/api/v1"

    def test_get_migration_path_returns_path_or_none(self):
        from specivo.core.plugin import BasePluginConfig

        class WithMigrations(BasePluginConfig):
            @property
            def name(self) -> str:
                return "test.migrations"

            def get_migration_path(self) -> Path | None:
                return Path("/fake/migrations/versions")

        plugin = WithMigrations()
        result = plugin.get_migration_path()
        assert isinstance(result, Path)
        assert str(result) == "/fake/migrations/versions"


# ---------------------------------------------------------------------------
# PLUGIN_API_VERSION constant
# ---------------------------------------------------------------------------


class TestPluginApiVersion:
    def test_plugin_api_version_constant_exists(self):
        from specivo.core.plugin_manager import PLUGIN_API_VERSION

        assert isinstance(PLUGIN_API_VERSION, str)
        # Must be a semver-like string (at least "MAJOR.MINOR")
        parts = PLUGIN_API_VERSION.split(".")
        assert len(parts) >= 2
        assert all(part.isdigit() for part in parts)
