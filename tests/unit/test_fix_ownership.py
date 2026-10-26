"""Decision and race tests for ``specivo.ops.fix_ownership``, without root.

``fchown`` is replaced by a recorder, so these check which inodes the helper
*would* hand to the app user. The files are owned by the user running the
tests, who therefore plays the installer (the data directory's owner). The
real owner changes, which need root, are in test_entrypoint_permissions.py.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from specivo.ops import fix_ownership
from specivo.ops.fix_ownership import Fixer

pytestmark = pytest.mark.unit

ME = os.geteuid()
# A uid that is not the test user, who plays the installer.
APP = 10001 if ME != 10001 else 10002
ROOT = Path(__file__).resolve().parents[2]


class Recorder:
    def __init__(self) -> None:
        self.inodes: list[int] = []
        self.calls: list[tuple[int, int, int]] = []

    def __call__(self, fd: int, uid: int, gid: int) -> None:
        self.inodes.append(os.fstat(fd).st_ino)
        self.calls.append((os.fstat(fd).st_ino, uid, gid))


def _ino(path: Path) -> int:
    return path.lstat().st_ino


def _fixer(**kwargs) -> tuple[Fixer, Recorder]:
    recorder = Recorder()
    return Fixer(app_uid=APP, app_gid=APP, fchown=recorder, **kwargs), recorder


@pytest.fixture
def data(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "attachments" / "sub").mkdir(parents=True)
    (root / "attachments" / "a.bin").write_text("a")
    (root / "attachments" / "sub" / "b.bin").write_text("b")
    (root / "models").mkdir()
    (root / "models" / "m.onnx").write_text("m")
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """Something the helper must never touch."""
    target = tmp_path / "outside"
    target.mkdir()
    (target / "secret").write_text("s")
    return target


def _outside_inodes(outside: Path) -> set[int]:
    return {_ino(outside), _ino(outside / "secret")}


def test_the_managed_directories_match_the_entrypoint():
    script = (ROOT / "scripts" / "entrypoint.sh").read_text()
    app_dirs = re.search(r'^APP_DIRS="([^"]+)"', script, re.MULTILINE)
    assert app_dirs
    names = [name for name, _ in fix_ownership.MANAGED] + [f"{fix_ownership.LOGS}/{fix_ownership.LOGS_APP[0]}"]
    assert app_dirs.group(1).split() == names


def test_files_and_directories_are_handed_over_children_first(data: Path):
    fixer, rec = _fixer()
    fixer.run(str(data))
    tree = data / "attachments"
    for path in (tree, tree / "a.bin", tree / "sub", tree / "sub" / "b.bin", data / "models" / "m.onnx"):
        assert _ino(path) in rec.inodes, path
    assert rec.inodes.index(_ino(tree / "sub" / "b.bin")) < rec.inodes.index(_ino(tree / "sub"))
    assert rec.inodes.index(_ino(tree / "sub")) < rec.inodes.index(_ino(tree))
    assert all(uid == APP and gid == APP for _, uid, gid in rec.calls)
    assert _ino(data) not in rec.inodes  # the data directory is never the app's


def test_missing_directories_are_created_with_their_modes(data: Path):
    fixer, rec = _fixer()
    fixer.run(str(data))
    for rel, mode in (("avatars", 0o755), ("state", 0o700), ("logs/app", 0o755)):
        assert (data / rel).is_dir()
        assert (data / rel).stat().st_mode & 0o777 == mode, rel
        assert _ino(data / rel) in rec.inodes
    assert _ino(data / "logs") not in rec.inodes


def test_a_world_writable_data_directory_is_closed(data: Path):
    os.chmod(data, 0o777)
    fixer, _ = _fixer()
    fixer.run(str(data))
    assert data.stat().st_mode & 0o002 == 0
    assert any("world-writable" in w for w in fixer.warnings)


def test_a_data_directory_owned_by_the_app_goes_back_to_root(data: Path):
    recorder = Recorder()
    fixer = Fixer(app_uid=ME, app_gid=os.getegid(), fchown=recorder)
    fixer.run(str(data))
    assert (_ino(data), 0, 0) in recorder.calls


def test_a_symlinked_managed_directory_is_not_followed(data: Path, outside: Path):
    (data / "attachments").rename(data / "attachments.bak")
    (data / "attachments").symlink_to(outside)
    fixer, rec = _fixer()
    fixer.run(str(data))
    assert not _outside_inodes(outside) & set(rec.inodes)
    assert any("attachments is a symlink" in w for w in fixer.warnings)


def test_symlinks_inside_are_not_followed(data: Path, outside: Path):
    (data / "attachments" / "dir-link").symlink_to(outside)
    (data / "attachments" / "file-link").symlink_to(outside / "secret")
    fixer, rec = _fixer()
    fixer.run(str(data))
    assert not _outside_inodes(outside) & set(rec.inodes)
    assert _ino(data / "attachments" / "dir-link") not in rec.inodes


def test_hard_linked_files_keep_their_owner(data: Path, outside: Path):
    os.link(outside / "secret", data / "attachments" / "hardlink.bin")
    fixer, rec = _fixer()
    fixer.run(str(data))
    assert _ino(outside / "secret") not in rec.inodes
    assert any("hard-linked files" in w for w in fixer.warnings)
    assert _ino(data / "attachments" / "a.bin") in rec.inodes


def test_app_owned_directories_inside_a_tree_are_not_entered(data: Path, monkeypatch: pytest.MonkeyPatch):
    # The test user plays the app here, so "sub" is a directory the app owns,
    # and every entry counts as one of root's for the refusal check.
    recorder = Recorder()
    fixer = Fixer(app_uid=ME, app_gid=os.getegid(), fchown=recorder)
    fixer.data_dev = data.stat().st_dev
    monkeypatch.setattr(fixer, "may_change", lambda uid: True)
    fd = os.open(data / "attachments", os.O_RDONLY | os.O_DIRECTORY)
    try:
        fixer.walk(fd, "attachments")
    finally:
        os.close(fd)
    assert _ino(data / "attachments" / "sub" / "b.bin") not in recorder.inodes
    assert any("belongs to the app but holds files of root" in w and "--all" in w for w in fixer.warnings)


def test_all_enters_app_owned_directories(data: Path, monkeypatch: pytest.MonkeyPatch):
    recorder = Recorder()
    fixer = Fixer(app_uid=ME, app_gid=os.getegid(), deep=True, fchown=recorder)
    fixer.data_dev = data.stat().st_dev
    monkeypatch.setattr(fixer, "may_change", lambda uid: True)
    fd = os.open(data / "attachments", os.O_RDONLY | os.O_DIRECTORY)
    try:
        fixer.walk(fd, "attachments")
    finally:
        os.close(fd)
    assert _ino(data / "attachments" / "sub" / "b.bin") in recorder.inodes


# -- races --------------------------------------------------------------------


def _swap_for_symlink(target: Path):
    """A hook that replaces the entry it is called for with a symlink to *target*."""

    def hook(stage: str, dir_fd: int, name: str, *, want_stage: str, want_name: str) -> None:
        if stage == want_stage and name == want_name:
            os.rename(name, f"{name}.moved", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.symlink(str(target), name, dir_fd=dir_fd)

    return hook


@pytest.mark.parametrize("stage", ["lstat", "open"])
def test_a_directory_swapped_for_a_symlink_mid_walk_redirects_nothing(
    data: Path, outside: Path, monkeypatch: pytest.MonkeyPatch, stage: str
):
    hook = _swap_for_symlink(outside)
    monkeypatch.setattr(fix_ownership, "_hook", lambda s, fd, n: hook(s, fd, n, want_stage=stage, want_name="sub"))
    fixer, rec = _fixer()
    fixer.run(str(data))
    assert not _outside_inodes(outside) & set(rec.inodes)
    if stage == "lstat":
        assert any("changed while it was being checked" in w for w in fixer.warnings)
    else:
        # The fd still names the original directory, which is what gets fixed.
        assert _ino(data / "attachments" / "sub.moved" / "b.bin") in rec.inodes


@pytest.mark.parametrize("stage", ["lstat", "open"])
def test_a_file_swapped_mid_walk_redirects_nothing(
    data: Path, outside: Path, monkeypatch: pytest.MonkeyPatch, stage: str
):
    hook = _swap_for_symlink(outside / "secret")
    monkeypatch.setattr(fix_ownership, "_hook", lambda s, fd, n: hook(s, fd, n, want_stage=stage, want_name="a.bin"))
    fixer, rec = _fixer()
    fixer.run(str(data))
    assert not _outside_inodes(outside) & set(rec.inodes)


def test_a_file_swapped_for_a_hard_link_is_skipped(data: Path, outside: Path, monkeypatch: pytest.MonkeyPatch):
    def hook(stage: str, dir_fd: int, name: str) -> None:
        if stage == "lstat" and name == "a.bin":
            os.unlink(name, dir_fd=dir_fd)
            os.link(outside / "secret", name, dst_dir_fd=dir_fd)

    monkeypatch.setattr(fix_ownership, "_hook", hook)
    fixer, rec = _fixer()
    fixer.run(str(data))
    assert _ino(outside / "secret") not in rec.inodes


# -- bootstrap file -------------------------------------------------------------


def _move(data: Path, fixer: Fixer) -> None:
    (data / "state").mkdir(exist_ok=True)
    fixer.data_dev = data.stat().st_dev
    data_fd = os.open(data, os.O_RDONLY | os.O_DIRECTORY)
    state_fd = os.open(data / "state", os.O_RDONLY | os.O_DIRECTORY)
    try:
        fixer.move_bootstrap(data_fd, state_fd, str(data))
    finally:
        os.close(state_fd)
        os.close(data_fd)


def test_bootstrap_file_is_copied_exclusively_and_the_source_removed(data: Path):
    (data / ".bootstrap.json").write_text('{"login": "admin"}')
    fixer, rec = _fixer()
    _move(data, fixer)
    dst = data / "state" / "bootstrap.json"
    assert dst.read_text() == '{"login": "admin"}'
    assert dst.stat().st_mode & 0o777 == 0o600
    assert _ino(dst) in rec.inodes
    assert not (data / ".bootstrap.json").exists()


def test_an_existing_destination_is_never_overwritten(data: Path):
    (data / ".bootstrap.json").write_text("new")
    (data / "state").mkdir()
    (data / "state" / "bootstrap.json").write_text("old")
    fixer, _ = _fixer()
    _move(data, fixer)
    assert (data / "state" / "bootstrap.json").read_text() == "old"
    assert (data / ".bootstrap.json").read_text() == "new"
    assert any("already exists" in w for w in fixer.warnings)


def test_a_symlink_planted_as_the_destination_is_not_followed(data: Path, outside: Path):
    (data / ".bootstrap.json").write_text("new")
    (data / "state").mkdir()
    (data / "state" / "bootstrap.json").symlink_to(outside / "secret")
    fixer, rec = _fixer()
    _move(data, fixer)
    assert (outside / "secret").read_text() == "s"
    assert _ino(outside / "secret") not in rec.inodes
    assert (data / ".bootstrap.json").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_a_bootstrap_source_that_is_not_a_plain_file_is_ignored(data: Path, outside: Path, kind: str):
    if kind == "symlink":
        (data / ".bootstrap.json").symlink_to(outside / "secret")
    else:
        os.link(outside / "secret", data / ".bootstrap.json")
    fixer, _ = _fixer()
    _move(data, fixer)
    assert not (data / "state" / "bootstrap.json").exists()
    assert any("not a plain file" in w for w in fixer.warnings)


def test_main_refuses_without_root(capsys: pytest.CaptureFixture[str]):
    if ME == 0:
        pytest.skip("running as root")
    assert fix_ownership.main(["--data-dir", "/nonexistent"]) == 2
