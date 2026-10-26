"""Plugin manager for discovering, loading, and orchestrating plugins.

Loads plugins from a list of dotted module paths, sorts by tier,
and delegates lifecycle hooks (services, features, routers, etc.).

:func:`bootstrap` is the one place a process loads its plugins and builds the
registries they feed that are not tied to the web app (the password hasher
registry). ``create_app()``, the import CLI, the admin CLI and the Celery
worker all call it, so a plugin is seen the same way by every process.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any

from specivo.core.features import FeatureRegistry
from specivo.core.services import ServiceRegistry

if TYPE_CHECKING:
    from specivo.core.config import Settings
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

    def collect_password_hashers(self) -> list[tuple[str, list[Any]]]:
        """Return ``(plugin_name, hashers)`` from each plugin's ``get_password_hashers()``."""
        return [(plugin.name, list(plugin.get_password_hashers())) for plugin in self._plugins]


def _register_core_services(pm: PluginManager) -> None:
    """Register core services first, so that plugins can override them."""
    from specivo.services.api_key_service import ApiKeyService
    from specivo.services.journal_service import JournalService
    from specivo.services.notification_service import NotificationService
    from specivo.services.workflow_service import WorkflowService

    pm.service_registry.register("journal", JournalService)
    pm.service_registry.register("api_key", ApiKeyService)
    pm.service_registry.register("workflow", WorkflowService)
    pm.service_registry.register("notification", NotificationService)


def bootstrap(settings: Settings | None = None) -> PluginManager:
    """Load the configured plugins and build the process-wide registries.

    Loads ``INSTALLED_PLUGINS``, registers their services and features, and
    builds and installs the password hasher registry from the built-in
    hashers, every plugin's ``get_password_hashers()`` and the
    ``PASSWORD_HASHERS`` setting. Returns the plugin manager; anything specific
    to the web app (routers, static files, templates) is left to the caller.

    Raises ``ImportError`` for a plugin that cannot be imported and
    ``PasswordHasherError`` for a hasher with an invalid, reserved or
    duplicate id, so a misconfigured process fails at start rather than at the
    first sign-in.
    """
    from specivo.core.config import get_settings
    from specivo.services.password_hashers import build_password_hasher_registry, install_password_hasher_registry

    settings = settings or get_settings()
    pm = PluginManager()
    _register_core_services(pm)
    if settings.installed_plugins:
        pm.load_plugins(settings.installed_plugins)
    pm.register_services()
    pm.register_features()

    registry = build_password_hasher_registry(
        plugin_hashers=pm.collect_password_hashers(),
        setting_paths=settings.password_hashers,
    )
    install_password_hasher_registry(registry)

    unknown = sorted(set(settings.legacy_password_algorithms) - set(registry.legacy()))
    if unknown:
        logger.warning(
            "LEGACY_PASSWORD_ALGORITHMS lists %s, which no hasher provides; registered: %s",
            ", ".join(unknown),
            ", ".join(sorted(registry.legacy())) or "none",
        )
    return pm
