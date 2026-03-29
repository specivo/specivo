"""Feature registry for tier-gated functionality.

Plugins register features they provide; application code checks feature
availability before enabling optional behaviour.
"""

from __future__ import annotations


def get_feature_registry() -> FeatureRegistry:
    """Return the application-wide FeatureRegistry singleton.

    Delegates to ``get_plugin_manager().feature_registry``.  This avoids
    circular imports by deferring the import until call time.
    """
    from specivo.main import get_plugin_manager

    return get_plugin_manager().feature_registry


def has_feature(feature: str) -> bool:
    """Convenience: check if *feature* is registered in the global registry."""
    return get_feature_registry().has_feature(feature)


class FeatureRegistry:
    """Registry of features provided by loaded plugins.

    Each feature is a string key mapped to the plugin name that provides it.
    Registration is idempotent -- the last plugin to register wins.
    """

    def __init__(self) -> None:
        self._features: dict[str, str] = {}

    def register(self, feature: str, provider: str) -> None:
        """Register *feature* as provided by *provider*.

        Idempotent: calling twice with different providers silently
        overwrites (last plugin wins).
        """
        self._features[feature] = provider

    def has_feature(self, feature: str) -> bool:
        """Return ``True`` if *feature* has been registered."""
        return feature in self._features

    def list_features(self) -> dict[str, str]:
        """Return a mapping of all registered features to their providers."""
        return dict(self._features)

    def require_feature(self, feature: str) -> None:
        """Raise ``RuntimeError`` if *feature* is not available."""
        if not self.has_feature(feature):
            raise RuntimeError(f"Feature '{feature}' is not available")
