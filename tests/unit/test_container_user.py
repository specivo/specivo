"""The container runs the app as an unprivileged user.

The image's last USER is a fixed non-root UID; only the one-shot ``init-data``
service starts as root, to give an existing install's data paths to that user.
Nothing the app writes at runtime may live under /app, which that user cannot
write to.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

import pytest
import yaml

from specivo.tasks import celery_app

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
APP_SERVICES = ("api", "celery-worker", "celery-embeddings", "celery-beat")


def _services(*files: str) -> dict:
    services: dict = {}
    for name in files:
        for service, spec in yaml.safe_load((ROOT / name).read_text())["services"].items():
            services.setdefault(service, {}).update(spec)
    return services


def _final_stage() -> str:
    text = (ROOT / "Dockerfile").read_text()
    return text[text.rindex("\nFROM ") :]


def test_image_ends_as_a_fixed_non_root_user():
    stage = _final_stage()
    users = re.findall(r"^USER\s+(\S+)", stage, re.MULTILINE)
    assert users, "the runtime stage has no USER"
    assert users[-1] == "$SPECIVO_UID:$SPECIVO_GID"
    assert "ARG SPECIVO_UID=10001" in stage
    assert "ARG SPECIVO_GID=10001" in stage


def test_image_has_a_healthcheck():
    assert re.search(r"^HEALTHCHECK\b.*\n\s+CMD \[\"specivo-healthcheck\"\]", _final_stage(), re.MULTILINE)


def test_init_data_runs_as_root_and_only_fixes_permissions():
    services = _services("docker-compose.yml")
    init = services["init-data"]
    assert init["user"] == "0:0"
    assert init["command"] == "fix-permissions"
    assert init["network_mode"] == "none"
    assert init["volumes"] == services["api"]["volumes"]
    assert services["api"]["depends_on"]["init-data"]["condition"] == "service_completed_successfully"


@pytest.mark.parametrize("files", [("docker-compose.yml",), ("docker-compose.yml", "docker-compose.dev.yml")])
def test_app_services_run_as_the_image_user(files):
    services = _services(*files)
    for name in APP_SERVICES:
        assert "user" not in services[name], name
    # The ownership fix runs from the same image as the app.
    assert services["init-data"]["image"] == services["api"]["image"]


def test_beat_schedule_is_not_written_to_the_working_directory():
    path = celery_app.conf.beat_schedule_filename
    assert os.path.isabs(path)
    assert not path.startswith("/app/")


def _load_healthcheck():
    spec = importlib.util.spec_from_file_location("specivo_healthcheck", ROOT / "scripts" / "healthcheck.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_healthcheck_passes_for_containers_without_a_web_server(monkeypatch):
    hc = _load_healthcheck()
    monkeypatch.setattr(hc, "_pid1_args", lambda: ["/app/.venv/bin/python", "/app/.venv/bin/celery", "-A", "x"])
    monkeypatch.setattr(hc.urllib.request, "urlopen", lambda *a, **k: pytest.fail("must not probe HTTP"))
    assert hc.main() == 0


def test_healthcheck_probes_the_uvicorn_port_behind_the_stealth_prefix(monkeypatch):
    hc = _load_healthcheck()
    monkeypatch.setattr(
        hc, "_pid1_args", lambda: ["/app/.venv/bin/python", "/app/.venv/bin/uvicorn", "app", "--port", "8123"]
    )
    monkeypatch.setenv("STEALTH_PREFIX", "/hidden/")
    seen: list[str] = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(url, timeout):
        seen.append(url)
        return _Resp()

    monkeypatch.setattr(hc.urllib.request, "urlopen", _urlopen)
    assert hc.main() == 0
    assert seen == ["http://127.0.0.1:8123/hidden/health/"]


def test_healthcheck_fails_when_the_endpoint_is_down(monkeypatch):
    hc = _load_healthcheck()
    monkeypatch.setattr(hc, "_pid1_args", lambda: ["uvicorn", "app"])

    def _urlopen(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(hc.urllib.request, "urlopen", _urlopen)
    assert hc.main() == 1
