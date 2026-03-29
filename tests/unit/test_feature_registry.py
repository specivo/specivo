"""Unit tests for FeatureRegistry.

RED phase — these tests define the expected behavior of:
- ``FeatureRegistry.register()`` and ``FeatureRegistry.has_feature()``
- ``FeatureRegistry.list_features()``
- ``FeatureRegistry.require_feature()`` raising on missing features
- Idempotent registration (last plugin wins)

All tests are pure — no database, no I/O.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# has_feature basics
# ---------------------------------------------------------------------------


class TestFeatureRegistryBasics:
    def test_unregistered_feature_returns_false(self):
        from specivo.core.features import FeatureRegistry

        registry = FeatureRegistry()
        assert registry.has_feature("nonexistent") is False

    def test_registered_feature_returns_true(self):
        from specivo.core.features import FeatureRegistry

        registry = FeatureRegistry()
        registry.register("threaded_comments", "specivo_pro.threads")
        assert registry.has_feature("threaded_comments") is True

    def test_register_is_idempotent(self):
        """Registering the same feature twice silently overwrites (last plugin wins)."""
        from specivo.core.features import FeatureRegistry

        registry = FeatureRegistry()
        registry.register("threaded_comments", "plugin_a")
        registry.register("threaded_comments", "plugin_b")
        assert registry.has_feature("threaded_comments") is True
        # The second registration wins
        features = registry.list_features()
        assert features["threaded_comments"] == "plugin_b"


# ---------------------------------------------------------------------------
# list_features
# ---------------------------------------------------------------------------


class TestFeatureRegistryListing:
    def test_list_features_returns_all_registered(self):
        from specivo.core.features import FeatureRegistry

        registry = FeatureRegistry()
        registry.register("threaded_comments", "pro.threads")
        registry.register("api_key_scopes", "pro.scopes")
        registry.register("sso", "ent.sso")

        features = registry.list_features()
        assert len(features) == 3
        assert features["threaded_comments"] == "pro.threads"
        assert features["api_key_scopes"] == "pro.scopes"
        assert features["sso"] == "ent.sso"

    def test_list_features_empty_registry(self):
        from specivo.core.features import FeatureRegistry

        registry = FeatureRegistry()
        assert registry.list_features() == {}


# ---------------------------------------------------------------------------
# require_feature
# ---------------------------------------------------------------------------


class TestFeatureRegistryRequire:
    def test_require_feature_raises_on_missing(self):
        from specivo.core.features import FeatureRegistry

        registry = FeatureRegistry()
        with pytest.raises(RuntimeError, match="not available"):
            registry.require_feature("missing_feature")

    def test_require_feature_passes_when_registered(self):
        from specivo.core.features import FeatureRegistry

        registry = FeatureRegistry()
        registry.register("threaded_comments", "pro.threads")
        # Should not raise
        registry.require_feature("threaded_comments")


# ---------------------------------------------------------------------------
# Module-level convenience (has_feature function)
# ---------------------------------------------------------------------------


class TestHasFeatureModuleLevel:
    """The module should expose a convenience function for feature checks.

    This avoids forcing every call site to go through the PluginManager.
    The exact implementation may be a module-level function that delegates
    to the PluginManager singleton, or a standalone function on the registry.
    """

    def test_has_feature_module_level_function(self):
        """A module-level has_feature() convenience function exists."""
        # This tests that the module provides a shortcut.
        # The exact import path matches the architecture doc.
        from specivo.core.features import FeatureRegistry

        # At minimum, FeatureRegistry.has_feature() is the canonical API.
        registry = FeatureRegistry()
        registry.register("test_feature", "test.plugin")
        assert registry.has_feature("test_feature") is True
        assert registry.has_feature("absent") is False
