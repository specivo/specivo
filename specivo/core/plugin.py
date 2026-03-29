"""Plugin configuration protocol and base class.

Defines the contract that all Specivo plugins must satisfy:
- ``PluginConfig`` — runtime-checkable Protocol with name, tier, version.
- ``BasePluginConfig`` — abstract base with no-op defaults for all hooks.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PluginConfig(Protocol):
    """Contract that every Specivo plugin must satisfy."""

    @property
    def name(self) -> str: ...

    @property
    def tier(self) -> str: ...

    @property
    def version(self) -> str: ...

    def get_models(self) -> list: ...

    def get_routers(self, prefix: str) -> list: ...

    def get_services(self, registry: Any) -> None: ...

    def get_features(self) -> list[str]: ...

    def get_celery_tasks(self, celery_app: Any) -> None: ...

    def get_migration_path(self) -> Path | None: ...

    def get_template_dirs(self) -> list: ...

    def get_static_dirs(self) -> list[tuple[Path, str]]: ...

    def get_static_assets(self) -> dict[str, list[str]]: ...

    def get_locale_dirs(self) -> list[tuple[Path, str]]: ...

    def on_startup(self, app: Any) -> None: ...


class BasePluginConfig(ABC):
    """Abstract base class providing no-op defaults for all plugin hooks.

    Subclasses must implement ``name`` and ``tier`` properties.
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def tier(self) -> str: ...

    @property
    def version(self) -> str:
        return "0.0.0"

    def get_models(self) -> list:
        return []

    def get_routers(self, prefix: str) -> list:
        return []

    def get_services(self, registry: Any) -> None:
        pass

    def get_features(self) -> list[str]:
        return []

    def get_celery_tasks(self, celery_app: Any) -> None:
        pass

    def get_migration_path(self) -> Path | None:
        return None

    def get_template_dirs(self) -> list:
        return []

    def get_static_dirs(self) -> list[tuple[Path, str]]:
        """Return ``(directory_path, url_mount_path)`` tuples for static files."""
        return []

    def get_static_assets(self) -> dict[str, list[str]]:
        """Return ``{"css": [...urls], "js": [...urls]}`` to include in base template."""
        return {"css": [], "js": []}

    def get_locale_dirs(self) -> list[tuple[Path, str]]:
        """Return ``(locale_directory, domain)`` tuples for translation catalogs."""
        return []

    def on_startup(self, app: Any) -> None:
        pass
