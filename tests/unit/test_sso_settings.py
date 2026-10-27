"""Instance-wide SSO settings: bounds, and the startup warning for insecure HTTP."""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from specivo.core.config import SSO_MAX_CLOCK_SKEW_SECONDS, Settings
from specivo.services.sso.log import log_startup_warnings

pytestmark = pytest.mark.unit

_BASE = {
    "database_url": "postgresql+asyncpg://u:p@localhost/db",
    "redis_url": "redis://localhost:6379/0",
    "secret_key": "x" * 40,
}


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **_BASE, **overrides)  # type: ignore[arg-type]


def test_defaults():
    s = _settings()
    assert s.sso_force_local_login is False
    assert s.sso_allow_insecure_http is False
    assert s.sso_http_timeout_seconds == 5.0
    assert s.sso_clock_skew_seconds == 60


def test_clock_skew_is_capped():
    assert _settings(sso_clock_skew_seconds=10_000).sso_clock_skew_seconds == SSO_MAX_CLOCK_SKEW_SECONDS


@pytest.mark.parametrize("field,value", [("sso_clock_skew_seconds", -1), ("sso_http_timeout_seconds", 0)])
def test_out_of_bounds_rejected(field: str, value: object):
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_insecure_http_logs_startup_warning(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING, logger="specivo.sso")
    log_startup_warnings(_settings(sso_allow_insecure_http=True))
    records = [r for r in caplog.records if r.name == "specivo.sso" and r.levelno == logging.WARNING]
    assert len(records) == 1
    assert "SSO_ALLOW_INSECURE_HTTP" in records[0].getMessage()


def test_no_warning_by_default(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING, logger="specivo.sso")
    log_startup_warnings(_settings())
    assert not [r for r in caplog.records if r.name == "specivo.sso"]


def test_lifespan_calls_startup_warnings():
    """The warning is only useful if the app actually emits it at startup."""
    import inspect

    from specivo import main

    assert "log_startup_warnings" in inspect.getsource(main.lifespan)
