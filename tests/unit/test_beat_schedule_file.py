"""Celery beat keeps its schedule (a pickle) in a private directory."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from specivo.tasks import beat, celery_app

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("CELERY_BEAT_SCHEDULE_FILE", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    beat._fallback_dir.cache_clear()
    return tmp_path / "home"


def test_app_uses_the_private_scheduler():
    assert celery_app.conf.beat_scheduler == "specivo.tasks.beat:PrivateStateScheduler"
    assert not celery_app.conf.beat_schedule_filename.startswith("/app/")


def test_default_is_in_the_home_state_directory(_home: Path):
    expected = _home / ".local" / "state" / "specivo" / "celerybeat-schedule"
    assert beat.default_schedule_file() == str(expected)
    assert beat.prepare_schedule_file(str(expected)) == str(expected)
    assert expected.parent.stat().st_mode & 0o777 == 0o700


def test_xdg_state_home_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert beat.default_schedule_file() == str(tmp_path / "state" / "specivo" / "celerybeat-schedule")


def test_an_open_state_directory_is_tightened(_home: Path):
    directory = _home / ".local" / "state" / "specivo"
    directory.mkdir(parents=True, mode=0o777)
    os.chmod(directory, 0o777)
    assert beat.prepare_schedule_file(None) == str(directory / "celerybeat-schedule")
    assert directory.stat().st_mode & 0o777 == 0o700


def test_a_symlinked_state_directory_is_not_used(_home: Path, tmp_path: Path):
    elsewhere = tmp_path / "shared"
    elsewhere.mkdir()
    directory = _home / ".local" / "state" / "specivo"
    directory.parent.mkdir(parents=True)
    directory.symlink_to(elsewhere)
    path = Path(beat.prepare_schedule_file(None))
    assert path.parent != directory
    assert path.parent.name.startswith("specivo-beat-")
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_unwritable_home_falls_back_to_one_private_temp_dir(_home: Path):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    _home.mkdir()
    os.chmod(_home, 0o500)
    try:
        first = beat.prepare_schedule_file(None)
        second = beat.prepare_schedule_file(None)
    finally:
        os.chmod(_home, 0o700)
    assert first == second
    assert "specivo-beat-" in first
    assert Path(first).parent.stat().st_mode & 0o777 == 0o700


def test_explicit_file_is_used_as_given(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    target = str(tmp_path / "custom" / "beat-schedule")
    monkeypatch.setenv("CELERY_BEAT_SCHEDULE_FILE", target)
    assert beat.default_schedule_file() == target
    assert beat.prepare_schedule_file(target) == target
