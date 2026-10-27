"""The E2E helpers must not switch on debug mode for the in-process test run.

pytest imports ``specivo.testing.e2e_base`` while collecting every run, E2E or
not. Debug mode drops the Secure flag from auth cookies, so a leaked
``DEBUG=true`` silently changes cookie and CSRF behaviour for any test that
rebuilds the settings with ``get_settings.cache_clear()``.
"""

import importlib
import os

import pytest

pytestmark = pytest.mark.unit


def test_importing_e2e_base_leaves_debug_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBUG", "false")
    import specivo.testing.e2e_base as e2e_base

    importlib.reload(e2e_base)

    assert os.environ["DEBUG"] == "false"


def test_the_e2e_server_still_runs_in_debug_mode() -> None:
    from specivo.testing.e2e_base import _E2E_SERVER_ENV

    assert _E2E_SERVER_ENV["DEBUG"] == "true"
