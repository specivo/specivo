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
import socket

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

# The Redmine fixture is started on demand and is not part of a normal test run.
# Its database port is probed at collection time so importer tests skip rather
# than fail when it is not up.
_REDMINE_FIXTURE_HOST = os.environ.get("REDMINE_FIXTURE_HOST", "127.0.0.1")
_REDMINE_FIXTURE_PORT = int(os.environ.get("REDMINE_FIXTURE_PG_PORT", "5444"))


def _fixture_is_running(host: str, port: int, timeout: float = 0.5) -> bool:
    """Whether something is listening, without importing a database driver."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests whose dependencies are absent.

    ``pro`` and ``enterprise`` need their plugins; ``redmine`` needs the
    importer's Redmine fixture, which is started on demand.
    """
    has_pro = "specivo_pro" in _INSTALLED_PLUGINS
    has_enterprise = "specivo_enterprise" in _INSTALLED_PLUGINS

    skip_pro = pytest.mark.skip(reason="requires specivo-pro plugin (not installed)")
    skip_ent = pytest.mark.skip(reason="requires specivo-enterprise plugin (not installed)")

    has_redmine_fixture = any(item.get_closest_marker("redmine") for item in items) and _fixture_is_running(
        _REDMINE_FIXTURE_HOST, _REDMINE_FIXTURE_PORT
    )
    skip_redmine = pytest.mark.skip(
        reason=f"Redmine fixture is not running on {_REDMINE_FIXTURE_HOST}:{_REDMINE_FIXTURE_PORT} "
        "(start it with: make redmine-fixture-up && make redmine-fixture-seed)"
    )

    for item in items:
        if not has_pro and item.get_closest_marker("pro"):
            item.add_marker(skip_pro)
        if not has_enterprise and item.get_closest_marker("enterprise"):
            item.add_marker(skip_ent)
        if not has_redmine_fixture and item.get_closest_marker("redmine"):
            item.add_marker(skip_redmine)


@pytest.fixture(autouse=True)
def _restore_plugin_manager():
    """Restore the global PluginManager singleton after each test.

    Integration tests that create custom apps (core-only, full-stack) via
    ``create_app()`` overwrite ``specivo.main._plugin_manager``. This fixture
    ensures it is restored so subsequent tests that use the default test app
    see the correct feature registry.

    The feature registry's *contents* are snapshotted too. Swapping the manager
    back does not undo a ``feature_registry.register(...)`` call made against
    the manager that stays in place, and a leaked feature silently changes
    behaviour for every later test in the same worker process: tier tests
    asserting core-only behaviour see an enterprise gate open and fail far away
    from the test that opened it.
    """
    import specivo.main as _main_mod

    original_pm = _main_mod._plugin_manager
    original_features = original_pm.feature_registry.list_features() if original_pm else {}
    yield
    _main_mod._plugin_manager = original_pm
    if original_pm is not None:
        registry = original_pm.feature_registry
        for feature in set(registry.list_features()) - set(original_features):
            registry.unregister(feature)
        for feature, provider in original_features.items():
            registry.register(feature, provider)
