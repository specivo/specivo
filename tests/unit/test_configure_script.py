"""``scripts/configure.py`` writes its secrets files readable by their owner only."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(os.name == "nt", reason="POSIX file modes"),
]

CONFIGURE_PY = Path(__file__).resolve().parents[2] / "scripts" / "configure.py"


@pytest.fixture
def configure(monkeypatch, tmp_path):
    """The configure module, pointed at an empty project directory, answering every prompt."""
    spec = importlib.util.spec_from_file_location("specivo_configure_under_test", CONFIGURE_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(module, "ask", lambda prompt, default="": default)
    monkeypatch.setattr(module, "ask_yes_no", lambda prompt, default=True: True)
    monkeypatch.setattr(module, "ask_password", lambda prompt, min_length=8: "admin-password-123")
    # A permissive umask, so the test cannot pass on the umask alone.
    old_umask = os.umask(0o022)
    try:
        yield module
    finally:
        os.umask(old_umask)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_secrets_files_are_owner_only(configure, tmp_path):
    configure.main()

    bootstrap = tmp_path / "specivo-data" / ".bootstrap.json"
    for path in (tmp_path / ".env", tmp_path / ".env.local", bootstrap):
        assert _mode(path) == 0o600, path.name
    assert "POSTGRES_PASSWORD=" in (tmp_path / ".env").read_text()
    assert (tmp_path / ".env.local").read_text().count("SECRET_KEY=") == 1
    assert json.loads(bootstrap.read_text())["password"] == "admin-password-123"


def test_rerun_narrows_an_existing_readable_file(configure, tmp_path):
    env_local = tmp_path / ".env.local"
    env_local.write_text("SECRET_KEY=old\n")
    env_local.chmod(0o644)

    configure.main()

    assert _mode(env_local) == 0o600
    assert "SECRET_KEY=old" not in env_local.read_text()


def test_write_private_creates_the_file_with_owner_only_mode(configure, tmp_path):
    target = tmp_path / "secret.txt"
    configure.write_private(target, "value\n")
    assert target.read_text() == "value\n"
    assert _mode(target) == 0o600
