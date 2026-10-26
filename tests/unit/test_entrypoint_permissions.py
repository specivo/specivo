"""The entrypoint's root step hands the app its directories, and nothing else.

These run ``scripts/entrypoint.sh fix-permissions`` for real, so they need
root and GNU coreutils/findutils (Linux): they are skipped elsewhere. Run them
in the image, for example::

    docker run --rm -u 0 --entrypoint sh -w /w -e UV_CACHE_DIR=/tmp/uvc \\
        -v "$PWD/scripts/entrypoint.sh":/w/scripts/entrypoint.sh:ro \\
        -v "$PWD/tests/unit/test_entrypoint_permissions.py":/w/tests/unit/test_entrypoint_permissions.py:ro \\
        specivo/specivo:dev -c "uv run -q --no-project --with pytest pytest -p no:cacheprovider \\
            --noconftest -c /dev/null -W ignore tests/unit/test_entrypoint_permissions.py"
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        not sys.platform.startswith("linux") or os.geteuid() != 0,
        reason="needs root on Linux to change file owners",
    ),
]

ENTRYPOINT = Path(__file__).resolve().parents[2] / "scripts" / "entrypoint.sh"
APP = 10001
DB = 999
INSTALLER = 1000


def _run(data: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "SPECIVO_DATA_PATH": str(data), "SPECIVO_UID": str(APP), "SPECIVO_GID": str(APP)}
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), "fix-permissions"], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result


def _uid(path: Path) -> int:
    return path.lstat().st_uid


def _write(path: Path, text: str = "x", uid: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.chown(path, uid, uid)
    return path


@pytest.fixture
def data(tmp_path: Path) -> Path:
    """A data directory as an install that ran as root leaves it."""
    root = tmp_path / "data"
    (root / "attachments" / "old").mkdir(parents=True)
    _write(root / "attachments" / "old" / "a.bin")
    _write(root / "models" / "m.onnx")
    _write(root / "logs" / "nginx" / "access.log")
    pg = root / "postgresql"
    _write(pg / "PG_VERSION", uid=DB)
    os.chown(pg, DB, DB)
    return root


def test_root_owned_install_gets_its_app_directories(data: Path):
    _run(data)
    assert _uid(data) == 0
    for rel in ("attachments", "attachments/old", "attachments/old/a.bin", "models", "models/m.onnx"):
        assert _uid(data / rel) == APP, rel
    for rel in ("avatars", "state", "logs/app"):
        assert (data / rel).is_dir() and _uid(data / rel) == APP, rel
    assert (data / "state").stat().st_mode & 0o777 == 0o700
    assert _uid(data / "logs") == 0
    assert _uid(data / "logs" / "nginx" / "access.log") == 0
    assert _uid(data / "postgresql") == DB
    assert _uid(data / "postgresql" / "PG_VERSION") == DB


def test_installer_owned_directories_are_taken_over(data: Path):
    os.chown(data, INSTALLER, INSTALLER)
    os.chown(data / "models", INSTALLER, INSTALLER)
    _run(data)
    assert _uid(data) == INSTALLER
    assert _uid(data / "models") == APP
    assert _uid(data / "models" / "m.onnx") == APP


def test_a_symlinked_app_directory_is_not_followed(data: Path):
    (data / "attachments").rename(data / "attachments.bak")
    (data / "attachments").symlink_to("postgresql")
    result = _run(data)
    assert "attachments is a symlink" in result.stderr
    assert _uid(data / "postgresql") == DB
    assert _uid(data / "postgresql" / "PG_VERSION") == DB


def test_symlinks_inside_are_not_followed(data: Path):
    (data / "attachments" / "link").symlink_to(data / "postgresql")
    (data / "attachments" / "rootfile").symlink_to(_write(data / "root-only.txt"))
    _run(data)
    assert _uid(data / "postgresql") == DB
    assert _uid(data / "root-only.txt") == 0


def test_hard_linked_files_keep_their_owner(data: Path):
    secret = _write(data / "root-only.txt")
    os.link(secret, data / "attachments" / "hardlink.bin")
    result = _run(data)
    assert "hard-linked files" in result.stderr
    assert _uid(secret) == 0
    assert _uid(data / "attachments" / "old" / "a.bin") == APP


def test_files_of_other_users_keep_their_owner(data: Path):
    _write(data / "attachments" / "db-file", uid=DB)
    _run(data)
    assert _uid(data / "attachments" / "db-file") == DB


def test_a_directory_owned_by_another_user_is_left_alone(data: Path):
    os.chown(data / "models", DB, DB)
    result = _run(data)
    assert "belongs to uid 999" in result.stderr
    assert _uid(data / "models") == DB
    assert _uid(data / "models" / "m.onnx") == 0


def test_an_app_owned_directory_is_not_walked(data: Path):
    _run(data)
    late = _write(data / "attachments" / "late-root-file")
    _run(data)
    assert _uid(late) == 0  # the cheap path: the top level is already right


def test_data_and_logs_directories_given_to_the_app_go_back_to_root(data: Path):
    os.chown(data, APP, APP)
    os.chown(data / "logs", APP, APP)
    os.chmod(data, 0o777)
    _run(data)
    assert _uid(data) == 0
    assert _uid(data / "logs") == 0
    assert data.stat().st_mode & 0o002 == 0


def test_bootstrap_file_moves_into_state(data: Path):
    src = _write(data / ".bootstrap.json", json.dumps({"login": "admin"}), uid=INSTALLER)
    os.chmod(src, 0o600)
    _run(data)
    moved = data / "state" / "bootstrap.json"
    assert not src.exists()
    assert _uid(moved) == APP
    assert moved.stat().st_mode & 0o777 == 0o600


def test_a_hard_linked_bootstrap_file_is_ignored(data: Path):
    secret = _write(data / "root-only.txt")
    os.link(secret, data / ".bootstrap.json")
    result = _run(data)
    assert "not a plain file" in result.stderr
    assert not (data / "state" / "bootstrap.json").exists()
    assert _uid(secret) == 0
