"""Plugin manager for discovering, loading, and orchestrating plugins.

Loads plugins from a list of dotted module paths, sorts by tier,
and delegates lifecycle hooks (services, features, routers, etc.).
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

from specivo.core.features import FeatureRegistry
from specivo.core.services import ServiceRegistry

if TYPE_CHECKING:
    from specivo.core.plugin import PluginConfig

logger = logging.getLogger(__name__)

PLUGIN_API_VERSION: str = "1.0"

_TIER_ORDER: dict[str, int] = {
    "core": 0,
    "pro": 1,
    "enterprise": 2,
}


class PluginManager:
    """Discovers, loads, and orchestrates Specivo plugins.

    Usage::

        pm = PluginManager()
        pm.load_plugins(["specivo_pro.plugin.Plugin"])
        pm.register_services()
        pm.register_features()
    """

    def __init__(self) -> None:
        self._plugins: list[PluginConfig] = []
        self._service_registry = ServiceRegistry()
        self._feature_registry = FeatureRegistry()

    @property
    def plugins(self) -> list[PluginConfig]:
        """Return loaded plugins sorted by tier."""
        return list(self._plugins)

    @property
    def service_registry(self) -> ServiceRegistry:
        return self._service_registry

    @property
    def feature_registry(self) -> FeatureRegistry:
        return self._feature_registry

    def load_plugins(self, plugin_paths: list[str]) -> None:
        """Import and instantiate plugins from dotted paths.

        Each path is ``"module.path.ClassName"`` -- the module is imported
        and ``ClassName`` is instantiated.  Plugins are sorted by tier
        after loading (core < pro < enterprise).

        Raises ``ImportError`` if a module cannot be imported.
        """
        plugins: list[PluginConfig] = []

        for path in plugin_paths:
            module_path, class_name = path.rsplit(".", 1)
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            instance = cls()
            plugins.append(instance)
            logger.info("Loaded plugin %s (tier=%s, version=%s)", instance.name, instance.tier, instance.version)

        # Sort by tier: core (0) < pro (1) < enterprise (2)
        plugins.sort(key=lambda p: _TIER_ORDER.get(p.tier, 99))
        self._plugins = plugins

    def register_services(self) -> None:
        """Call ``get_services()`` on each loaded plugin."""
        for plugin in self._plugins:
            plugin.get_services(self._service_registry)

    def register_features(self) -> None:
        """Call ``get_features()`` on each loaded plugin and register them."""
        for plugin in self._plugins:
            features = plugin.get_features()
            for feature in features:
                self._feature_registry.register(feature, plugin.name)
