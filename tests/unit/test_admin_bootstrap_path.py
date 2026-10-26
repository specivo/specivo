"""The admin bootstrap file is read from state/ first, then the installer's path."""

from __future__ import annotations

from pathlib import Path

import pytest

from specivo.cli import admin

pytestmark = pytest.mark.unit


def test_state_copy_wins_over_the_installer_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state, legacy = tmp_path / "state" / "bootstrap.json", tmp_path / ".bootstrap.json"
    monkeypatch.setattr(admin, "BOOTSTRAP_PATHS", (state, legacy))
    assert admin._bootstrap_path() is None
    legacy.write_text("{}")
    assert admin._bootstrap_path() == legacy
    state.parent.mkdir()
    state.write_text("{}")
    assert admin._bootstrap_path() == state


def test_the_container_paths():
    assert admin.BOOTSTRAP_PATHS == (Path("/app/data/state/bootstrap.json"), Path("/app/data/.bootstrap.json"))
