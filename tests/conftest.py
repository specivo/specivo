"""Shared test fixtures for specivo-core.

Uses a real PostgreSQL instance (docker-compose.test.yml on port 5433).

Isolation strategy: **transaction rollback** (not TRUNCATE).

Each test runs inside a top-level transaction that is rolled back after
the test completes. This is much faster than TRUNCATE (~1ms vs ~300ms
per test) because no DDL or data destruction occurs.

All fixtures are defined in specivo.testing.conftest_base and re-exported
here so pytest discovers them automatically.

The suite runs without plugins.
"""

import os
import re
import urllib.request

import pytest
import pytest_asyncio

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
from specivo.testing.oidc_mock import mock_idp  # noqa: F401
from specivo.testing.sso import sso_login, sso_provider  # noqa: F401

# ---------------------------------------------------------------------------
# Auto-skip tests whose dependencies are absent
# ---------------------------------------------------------------------------

# The disposable OIDC IdP (tests/fixtures/sso/docker-compose.yml) is also started on
# demand. Its discovery document is fetched once at collection time, and only
# when an ``sso_idp`` test was collected.
_SSO_TEST_ISSUER = os.environ.get("SSO_TEST_ISSUER", "http://localhost:8033/realms/specivo-test")


def _issuer_is_up(issuer: str, timeout: float = 1.0) -> bool:
    """Whether the issuer serves its OpenID discovery document."""
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # fixed http(s) test URL
            return bool(response.status == 200)
    except (OSError, ValueError):
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests whose dependencies are absent, and queue the AI usage tests.

    ``sso_idp`` needs the disposable IdP, which is started on demand. The AI
    usage tests get ``_ai_tests_lock`` first (see below).
    """
    has_sso_idp = any(item.get_closest_marker("sso_idp") for item in items) and _issuer_is_up(_SSO_TEST_ISSUER)
    skip_sso_idp = pytest.mark.skip(
        reason=f"SSO test IdP is not serving {_SSO_TEST_ISSUER} (start it with: make sso-idp-up)"
    )
    for item in items:
        if not has_sso_idp and item.get_closest_marker("sso_idp"):
            item.add_marker(skip_sso_idp)
        names = getattr(item, "fixturenames", None)
        if names is not None and _AI_TEST_MODULE.match(item.path.name) and "db_session" in names:
            names.insert(0, "_ai_tests_lock")


@pytest.fixture(autouse=True)
def _restore_plugin_manager():
    """Restore the global PluginManager singleton after each test.

    Integration tests that create custom apps (core-only, full-stack) via
    ``create_app()`` overwrite ``specivo.main._plugin_manager``. This fixture
    ensures it is restored so subsequent tests that use the default test app
    see the plugin manager they expect.
    """
    import specivo.main as _main_mod

    original_pm = _main_mod._plugin_manager
    yield
    _main_mod._plugin_manager = original_pm


# ---------------------------------------------------------------------------
# AI usage tests across xdist workers
# ---------------------------------------------------------------------------
#
# The AI usage modules build overlapping worlds: the same invented prices
# (provider "testlab", which the exclusion constraint
# ex_ai_model_prices_no_overlap makes concurrent transactions wait on) and
# some fixed role names. Each test holds its transaction open until it ends,
# so two workers running such modules wait on each other's rows, and the
# waits form cycles that Postgres breaks with a deadlock error. Their tests
# take one transaction-scoped advisory lock first, before any fixture writes:
# they queue behind each other instead; every other test is unaffected.

_AI_TESTS_LOCK = 0x5A1_9E1CE  # an arbitrary key, the same in every worker
_AI_TEST_MODULE = re.compile(r"^test_(mcp_)?ai_")


@pytest_asyncio.fixture
async def _ai_tests_lock(db_session) -> None:  # noqa: F811 - the re-exported fixture
    from sqlalchemy import text

    await db_session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _AI_TESTS_LOCK})
