"""Unit tests for the PluginConfig protocol (name and version are required).

All tests are pure — no database, no I/O.
"""

from __future__ import annotations

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
