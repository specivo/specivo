"""APP_URL checks: which values are local, the host comparison, and the start-up log line."""

from __future__ import annotations

import inspect
import logging

import pytest

from specivo.core.app_url import (
    APP_URL_GUIDE_URL,
    app_url_problem,
    is_local_host,
    log_app_url_warning,
    request_host_differs,
)
from specivo.core.config import Settings

pytestmark = pytest.mark.unit

_BASE = {
    "database_url": "postgresql+asyncpg://u:p@localhost/db",
    "redis_url": "redis://localhost:6379/0",
    "secret_key": "x" * 40,
}


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **_BASE, **overrides)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "app_url",
    [
        "http://localhost:8030",
        "http://LOCALHOST:8030/",
        "http://localhost.:8030",
        "https://specivo.localhost",
        "http://127.0.0.1:8000",
        "http://127.1.2.3",
        "http://[::1]:8030",
        "http://0.0.0.0:8030",
        "http://[::]:8030",
    ],
)
def test_local_addresses_are_a_problem(app_url: str) -> None:
    assert app_url_problem(app_url) == "local"


@pytest.mark.parametrize("app_url", ["", "   ", "not a url", "https://", "/relative/path"])
def test_missing_host_counts_as_unset(app_url: str) -> None:
    assert app_url_problem(app_url) == "unset"


@pytest.mark.parametrize(
    "app_url",
    [
        "https://specivo.example.com",
        "https://specivo.example.com:8443/",
        "http://specivo.example.com",  # plain http on a real host is not this warning's business
        "https://192.0.2.10",
        "https://[2001:db8::1]",
        "https://localhost-tracker.example.com",
        "https://notlocalhost",
    ],
)
def test_real_addresses_are_fine(app_url: str) -> None:
    assert app_url_problem(app_url) is None


def test_default_setting_is_a_problem() -> None:
    assert app_url_problem(_settings().app_url) == "local"


def test_is_local_host_handles_empty() -> None:
    assert is_local_host(None) is False
    assert is_local_host("") is False


@pytest.mark.parametrize(
    "request_host,differs",
    [
        ("specivo.example.com", False),
        ("SPECIVO.example.com.", False),
        ("other.example.org", True),
        ("10.0.0.5", True),
        ("", False),
        (None, False),
    ],
)
def test_request_host_comparison(request_host: str | None, differs: bool) -> None:
    assert request_host_differs("https://specivo.example.com:8443", request_host) is differs


def _app_url_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "specivo.core.app_url" and r.levelno == logging.WARNING]


@pytest.mark.parametrize("app_url", ["http://localhost:8030", "http://127.0.0.1:9933"])
def test_startup_warning_when_production_app_url_is_local(caplog: pytest.LogCaptureFixture, app_url: str) -> None:
    caplog.set_level(logging.WARNING, logger="specivo.core.app_url")
    assert log_app_url_warning(_settings(debug=False, app_url=app_url)) is True
    (record,) = _app_url_records(caplog)
    message = record.getMessage()
    assert "APP_URL" in message
    assert app_url in message
    assert APP_URL_GUIDE_URL in message


def test_no_startup_warning_in_debug(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="specivo.core.app_url")
    assert log_app_url_warning(_settings(debug=True, app_url="http://localhost:8030")) is False
    assert not _app_url_records(caplog)


def test_no_startup_warning_for_a_real_address(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="specivo.core.app_url")
    assert log_app_url_warning(_settings(debug=False, app_url="https://specivo.example.com")) is False
    assert not _app_url_records(caplog)


def test_lifespan_logs_the_app_url_warning() -> None:
    from specivo import main

    assert "log_app_url_warning" in inspect.getsource(main.lifespan)


def test_worker_start_logs_the_app_url_warning(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import specivo.tasks as tasks
    from specivo.core import config

    assert "_warn_about_app_url" in inspect.getsource(tasks._bootstrap_worker)
    monkeypatch.setattr(config, "get_settings", lambda: _settings(debug=False, app_url="http://localhost:8030"))
    caplog.set_level(logging.WARNING, logger="specivo.core.app_url")
    tasks._warn_about_app_url()
    assert len(_app_url_records(caplog)) == 1


def test_worker_start_survives_a_settings_error(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import specivo.tasks as tasks
    from specivo.core import config

    def _broken() -> Settings:
        raise RuntimeError("no settings")

    monkeypatch.setattr(config, "get_settings", _broken)
    tasks._warn_about_app_url()  # must not raise
