"""Shared test fixtures for specivo-core.

Uses a real PostgreSQL instance (docker-compose.test.yml on port 5433).

Isolation strategy: **transaction rollback** (not TRUNCATE).

Each test runs inside a top-level transaction that is rolled back after
the test completes. This is much faster than TRUNCATE (~1ms vs ~300ms
per test) because no DDL or data destruction occurs.

All fixtures are defined in specivo.testing.conftest_base and re-exported
here so pytest discovers them automatically.

Core runs in core-only mode (no plugins). Pro and enterprise tests live
in their respective repos.
"""

import os

import pytest

from specivo.testing.conftest_base import (  # noqa: F401
    _create_test_app,
    _make_test_get_db,
    _test_connection,
    _test_lifespan,
    admin_client,
    agent_client,
    auth_client,
    client,
    db_engine,
    db_session,
    unauth_client,
)

# ---------------------------------------------------------------------------
# Auto-skip pro / enterprise tests in core-only mode
# ---------------------------------------------------------------------------

_INSTALLED_PLUGINS = os.environ.get("INSTALLED_PLUGINS", "[]")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests marked ``pro`` or ``enterprise`` when the corresponding
    plugins are not loaded (core-only CI)."""
    has_pro = "specivo_pro" in _INSTALLED_PLUGINS
    has_enterprise = "specivo_enterprise" in _INSTALLED_PLUGINS

    skip_pro = pytest.mark.skip(reason="requires specivo-pro plugin (not installed)")
    skip_ent = pytest.mark.skip(reason="requires specivo-enterprise plugin (not installed)")

    for item in items:
        if not has_pro and item.get_closest_marker("pro"):
            item.add_marker(skip_pro)
        if not has_enterprise and item.get_closest_marker("enterprise"):
            item.add_marker(skip_ent)


@pytest.fixture(autouse=True)
def _restore_plugin_manager():
    """Restore the global PluginManager singleton after each test.

    Integration tests that create custom apps (core-only, full-stack) via
    ``create_app()`` overwrite ``specivo.main._plugin_manager``. This fixture
    ensures it is restored so subsequent tests that use the default test app
    see the correct feature registry.
    """
    import specivo.main as _main_mod

    original_pm = _main_mod._plugin_manager
    yield
    _main_mod._plugin_manager = original_pm
