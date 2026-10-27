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


def test_importing_e2e_base_leaves_sso_settings_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSO_ALLOW_INSECURE_HTTP", raising=False)
    monkeypatch.delenv("APP_URL", raising=False)
    import specivo.testing.e2e_base as e2e_base

    importlib.reload(e2e_base)

    assert "SSO_ALLOW_INSECURE_HTTP" not in os.environ
    assert "APP_URL" not in os.environ


def test_the_e2e_server_matches_the_test_realm_redirect_uris() -> None:
    """The disposable Keycloak realm registers http://localhost:<port>, so APP_URL must use it."""
    from specivo.testing.e2e_base import _E2E_SERVER_ENV, E2E_SERVER_PORT

    assert _E2E_SERVER_ENV["APP_URL"] == f"http://localhost:{E2E_SERVER_PORT}"
    assert _E2E_SERVER_ENV["SSO_ALLOW_INSECURE_HTTP"] == "true"


def test_port_cleanup_kills_the_listener_but_not_a_connected_client() -> None:
    """Only the server on the port dies; a process merely connected to it lives.

    Docker Desktop's backend holds such connections when a container calls
    ``host.docker.internal:<port>``; killing it took the Docker engine down.
    """
    import socket
    import subprocess
    import sys
    import time

    from specivo.testing.e2e_base import kill_port_listeners

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    listener = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import socket,time;s=socket.socket();"
            "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
            f"s.bind(('127.0.0.1',{port}));s.listen();c,_=s.accept();time.sleep(60)",
        ]
    )
    client = None
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
                break
            except OSError:
                time.sleep(0.1)
        client = subprocess.Popen(
            [
                sys.executable,
                "-c",
                f"import socket,time;c=socket.create_connection(('127.0.0.1',{port}));time.sleep(60)",
            ]
        )
        time.sleep(0.5)

        kill_port_listeners(port)

        assert listener.wait(timeout=5) != 0
        assert client.poll() is None, "a process connected to the port must survive"
    finally:
        for proc in (listener, client):
            if proc is not None and proc.poll() is None:
                proc.kill()
