"""Unit tests for ServiceRegistry.

RED phase — these tests define the expected behavior of:
- ``ServiceRegistry.register()`` and ``ServiceRegistry.get()``
- ``ServiceRegistry.get_instance()`` returns a new instance
- ``ServiceRegistry.override()`` replaces an existing registration
- Error handling for unknown services and duplicate registrations

All tests are pure — no database, no I/O.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeService:
    """Minimal service class for testing."""

    pass


class FakeServiceOverride(FakeService):
    """Override that extends the original."""

    pass


class UnrelatedService:
    """Service that is NOT a subclass of FakeService."""

    pass


# ---------------------------------------------------------------------------
# Register and get
# ---------------------------------------------------------------------------


class TestServiceRegistryBasics:
    def test_register_and_get_class(self):
        """register() stores a class; get() returns it."""
        from specivo.core.services import ServiceRegistry

        registry = ServiceRegistry()
        registry.register("fake", FakeService)
        assert registry.get("fake") is FakeService

    def test_register_and_get_instance(self):
        """get_instance() returns a new instance of the registered class."""
        from specivo.core.services import ServiceRegistry

        registry = ServiceRegistry()
        registry.register("fake", FakeService)
        instance = registry.get_instance("fake")
        assert isinstance(instance, FakeService)

    def test_get_instance_returns_fresh_instance_each_call(self):
        """Each call to get_instance() creates a new object (stateless services)."""
        from specivo.core.services import ServiceRegistry

        registry = ServiceRegistry()
        registry.register("fake", FakeService)
        a = registry.get_instance("fake")
        b = registry.get_instance("fake")
        assert a is not b

    def test_get_class_returns_registered_class(self):
        """get() returns the class itself, not an instance."""
        from specivo.core.services import ServiceRegistry

        registry = ServiceRegistry()
        registry.register("fake", FakeService)
        cls = registry.get("fake")
        assert cls is FakeService
        assert isinstance(cls, type)

    def test_has_returns_true_for_registered(self):
        from specivo.core.services import ServiceRegistry

        registry = ServiceRegistry()
        registry.register("fake", FakeService)
        assert registry.has("fake") is True

    def test_has_returns_false_for_unregistered(self):
        from specivo.core.services import ServiceRegistry

        registry = ServiceRegistry()
        assert registry.has("nonexistent") is False


# ---------------------------------------------------------------------------
# Override
# ---------------------------------------------------------------------------


class TestServiceRegistryOverride:
    def test_override_replaces_implementation(self):
        """override() replaces the registered class."""
        from specivo.core.services import ServiceRegistry

        registry = ServiceRegistry()
        registry.register("fake", FakeService)
        registry.override("fake", FakeServiceOverride)
        assert registry.get("fake") is FakeServiceOverride

    def test_override_instance_uses_new_class(self):
        """After override, get_instance() returns the new class."""
        from specivo.core.services import ServiceRegistry

        registry = ServiceRegistry()
        registry.register("fake", FakeService)
        registry.override("fake", FakeServiceOverride)
        instance = registry.get_instance("fake")
        assert isinstance(instance, FakeServiceOverride)

    def test_override_unregistered_raises_error(self):
        """override() on a name that was never registered raises ValueError."""
        from specivo.core.services import ServiceRegistry

        registry = ServiceRegistry()
        with pytest.raises(ValueError, match="not registered"):
            registry.override("nonexistent", FakeServiceOverride)

    def test_duplicate_register_raises_error(self):
        """register() the same name twice raises ValueError."""
        from specivo.core.services import ServiceRegistry

        registry = ServiceRegistry()
        registry.register("fake", FakeService)
        with pytest.raises(ValueError, match="already registered"):
            registry.register("fake", FakeServiceOverride)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestServiceRegistryErrors:
    def test_get_unknown_service_raises_error(self):
        """get() for an unregistered service raises KeyError."""
        from specivo.core.services import ServiceRegistry

        registry = ServiceRegistry()
        with pytest.raises(KeyError, match="not found"):
            registry.get("nonexistent")

    def test_get_instance_unknown_service_raises_error(self):
        """get_instance() for an unregistered service raises KeyError."""
        from specivo.core.services import ServiceRegistry

        registry = ServiceRegistry()
        with pytest.raises(KeyError, match="not found"):
            registry.get_instance("nonexistent")


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class TestServiceRegistryListing:
    def test_list_services_returns_all_registered(self):
        from specivo.core.services import ServiceRegistry

        registry = ServiceRegistry()
        registry.register("alpha", FakeService)
        registry.register("beta", FakeServiceOverride)
        result = registry.list_services()
        assert "alpha" in result
        assert "beta" in result
        assert len(result) == 2
