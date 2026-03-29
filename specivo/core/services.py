"""Service registry for plugin-based dependency injection.

Provides a class-based registry where plugins register service implementations
and consumers look them up by name.
"""

from __future__ import annotations

from typing import Any


class ServiceRegistry:
    """Registry mapping service names to implementation classes.

    Services are registered by name and can be retrieved as classes
    or instantiated on demand.  ``override()`` allows higher-tier plugins
    to replace lower-tier implementations.
    """

    def __init__(self) -> None:
        self._services: dict[str, type] = {}

    def register(self, name: str, cls: type) -> None:
        """Register a service class under *name*.

        Raises ``ValueError`` if *name* is already registered.
        Use ``override()`` to replace an existing registration.
        """
        if name in self._services:
            raise ValueError(f"Service '{name}' is already registered")
        self._services[name] = cls

    def override(self, name: str, cls: type) -> None:
        """Replace an existing service registration.

        Raises ``ValueError`` if *name* was never registered.
        """
        if name not in self._services:
            raise ValueError(f"Service '{name}' is not registered")
        self._services[name] = cls

    def get(self, name: str) -> type:
        """Return the registered class for *name*.

        Raises ``KeyError`` if *name* is not found.
        """
        if name not in self._services:
            raise KeyError(f"Service '{name}' not found")
        return self._services[name]

    def get_instance(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Instantiate and return a new instance of the registered class.

        Raises ``KeyError`` if *name* is not found.
        """
        cls = self.get(name)
        return cls(*args, **kwargs)

    def has(self, name: str) -> bool:
        """Return ``True`` if *name* is registered."""
        return name in self._services

    def list_services(self) -> list[str]:
        """Return a list of all registered service names."""
        return list(self._services.keys())
