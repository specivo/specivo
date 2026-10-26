"""Give the app user its directories in the data directory, as root, safely.

``scripts/entrypoint.sh`` runs this while it is still root (the ``init-data``
compose service, or any container started with ``--user 0``)::

    python -I -m specivo.ops.fix_ownership [--all]

Policy:

- The data directory itself and ``logs/`` are not the app's. If an earlier
  build gave them to the app user they go back to root, and neither may be
  world-writable.
- The app owns ``attachments``, ``avatars``, ``models``, ``state`` (0700) and
  ``logs/app``. Missing ones are created.
- A managed directory whose top level already belongs to the app user is not
  walked, so restarts stay fast however many files it holds.
- Otherwise its tree is walked and only directories, and regular files with a
  single link, owned by root or by the data directory's owner (the uid that
  ran the installer) change owner. Symlinks are never followed or changed.
  Files of other users (the database's uid 999, say) keep their owner, and
  directories of other users are not entered.
- A hard-linked file keeps its owner and is reported: changing it would
  change every name it is linked under.
- The installer's ``.bootstrap.json`` moves into ``state/``.

Mechanism: nothing is done by path. The data directory is opened with
``O_NOFOLLOW``, every entry is opened relative to its parent's directory fd
with ``O_NOFOLLOW``, and owners and modes change only through ``fchown`` and
``fchmod`` on those fds, after an ``fstat`` confirms the fd is still the entry
that was looked at (same inode, same filesystem, an owner that may change,
one link). Renaming or replacing entries while this runs cannot redirect it
outside the tree.

What the app user can change concurrently is its own directories. They are
never entered by default: a directory owned by the app inside a tree being
fixed is skipped, with a warning when it holds files of root. ``--all``
enters them too; run it only with every app container stopped
(``api``, ``celery-worker``, ``celery-embeddings``, ``celery-beat``).

Standard library only, so it runs with ``python -I`` before anything else.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import os
import stat
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

DEFAULT_DATA_DIR = "/app/data"
# (name, mode) of the directories the app owns, relative to the data directory.
MANAGED = (("attachments", 0o755), ("avatars", 0o755), ("models", 0o755), ("state", 0o700))
LOGS = "logs"
LOGS_APP = ("app", 0o755)
BOOTSTRAP_SRC = ".bootstrap.json"
BOOTSTRAP_DST = "bootstrap.json"
MAX_BOOTSTRAP_BYTES = 64 * 1024
STOP_AND_RERUN = (
    "Stop all app containers (api, celery-worker, celery-embeddings, celery-beat), then run "
    "`docker compose run --rm init-data fix-permissions --all`."
)

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0) | _CLOEXEC


def _hook(stage: str, dir_fd: int, name: str) -> None:
    """Test seam, called between looking at an entry (``lstat``) and opening it,
    and between opening it (``open``) and acting on the fd."""


@contextlib.contextmanager
def _closing(fd: int) -> Iterator[int]:
    try:
        yield fd
    finally:
        os.close(fd)


@dataclass
class Fixer:
    app_uid: int
    app_gid: int
    deep: bool = False
    # Injected so the decision logic can be tested without root.
    fchown: Callable[[int, int, int], None] = os.fchown
    fchmod: Callable[[int, int], None] = os.fchmod
    warnings: list[str] = field(default_factory=list)
    changed: int = 0
    data_dev: int = -1
    data_owner: int = -1
    _hard_linked: list[str] = field(default_factory=list)

    # -- output -------------------------------------------------------------

    def info(self, message: str) -> None:
        print(message, flush=True)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        print(f"WARNING: {message}", file=sys.stderr, flush=True)

    # -- policy -------------------------------------------------------------

    def may_change(self, uid: int) -> bool:
        """Owners whose entries are handed to the app user."""
        return uid != self.app_uid and uid in (0, self.data_owner)

    # -- entry point --------------------------------------------------------

    def run(self, data_dir: str) -> None:
        try:
            data_fd = os.open(data_dir, _DIR_FLAGS)
        except FileNotFoundError:
            return
        except OSError as exc:
            self.warn(f"cannot open {data_dir} ({exc.strerror}); not changing anything.")
            return
        with _closing(data_fd):
            if not stat.S_ISDIR(os.fstat(data_fd).st_mode):
                return
            self.reclaim(data_fd, data_dir)
            st = os.fstat(data_fd)
            self.data_dev, self.data_owner = st.st_dev, st.st_uid
            for name, mode in MANAGED:
                self.manage(data_fd, name, mode, f"{data_dir}/{name}")
            logs_fd = self.root_dir(data_fd, LOGS, f"{data_dir}/{LOGS}")
            if logs_fd is not None:
                with _closing(logs_fd):
                    name, mode = LOGS_APP
                    self.manage(logs_fd, name, mode, f"{data_dir}/{LOGS}/{name}")
            state_fd = self.open_dir(data_fd, "state", f"{data_dir}/state", quiet=True)
            if state_fd is not None:
                with _closing(state_fd):
                    st = os.fstat(state_fd)
                    if st.st_uid == self.app_uid and st.st_dev == self.data_dev:
                        self.move_bootstrap(data_fd, state_fd, data_dir)

    # -- directories the app must not own ------------------------------------

    def reclaim(self, fd: int, label: str) -> None:
        st = os.fstat(fd)
        if st.st_uid == self.app_uid:
            self.info(f"Giving {label} back to root")
            self.fchown(fd, 0, 0)
        st = os.fstat(fd)
        if st.st_mode & stat.S_IWOTH:
            self.warn(f"{label} was world-writable; removing that.")
            self.fchmod(fd, stat.S_IMODE(st.st_mode) & ~stat.S_IWOTH)

    def root_dir(self, parent_fd: int, name: str, label: str) -> int | None:
        """Open (creating it as root if missing) a directory the app must not own."""
        try:
            fd = self.open_dir(parent_fd, name, label)
        except FileNotFoundError:
            os.mkdir(name, 0o755, dir_fd=parent_fd)
            fd = self.open_dir(parent_fd, name, label)
        if fd is None:
            return None
        st = os.fstat(fd)
        if st.st_dev != self.data_dev:
            self.warn(f"{label} is on another filesystem; not using it.")
            os.close(fd)
            return None
        self.reclaim(fd, label)
        return fd

    # -- directories the app owns --------------------------------------------

    def open_dir(self, parent_fd: int, name: str, label: str, quiet: bool = False) -> int | None:
        """Open a directory entry without following a symlink.

        Raises FileNotFoundError when it is missing; returns None (with a
        warning unless *quiet*) when it is a symlink or not a directory.
        """
        try:
            return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            if exc.errno not in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                raise
        if not quiet:
            try:
                is_link = stat.S_ISLNK(os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode)
            except OSError:
                is_link = False
            if is_link:
                self.warn(f"{label} is a symlink; not changing its owner. Mount the target there instead.")
            else:
                self.warn(f"{label} is not a directory; leaving it alone.")
        return None

    def manage(self, parent_fd: int, name: str, mode: int, label: str) -> None:
        try:
            fd = self.open_dir(parent_fd, name, label)
        except FileNotFoundError:
            self.create(parent_fd, name, mode, label)
            return
        if fd is None:
            return
        with _closing(fd):
            _hook("open", parent_fd, name)
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                self.warn(f"{label} is not a directory; leaving it alone.")
                return
            if st.st_dev != self.data_dev:
                self.warn(
                    f"{label} is on another filesystem; not changing its owner. "
                    f"Run: chown -R {self.app_uid}:{self.app_gid} on it."
                )
                return
            owned = st.st_uid == self.app_uid
            if owned and not self.deep:
                return  # the cheap path
            if not owned and not self.may_change(st.st_uid):
                self.warn(
                    f"{label} belongs to uid {st.st_uid}, not root or the data directory's owner; not changing it."
                )
                print(
                    f"  If the app should use it, run chown -R {self.app_uid}:{self.app_gid} on it on the host.",
                    file=sys.stderr,
                )
                return
            self.info(f"{'Checking' if owned else 'Setting owner of'} {label} (to {self.app_uid}:{self.app_gid})")
            self._hard_linked = []
            self.walk(fd, label)
            if not owned:
                self.fchown(fd, self.app_uid, self.app_gid)
                self.changed += 1
            if self._hard_linked:
                self.warn(f"hard-linked files in {label} kept their owner (fix them by hand if the app needs them):")
                for path in self._hard_linked[:5]:
                    print(path, file=sys.stderr)

    def create(self, parent_fd: int, name: str, mode: int, label: str) -> None:
        self.info(f"Creating {label}")
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        fd = self.open_dir(parent_fd, name, label)
        if fd is None:
            return
        with _closing(fd):
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
                self.warn(f"{label} changed while it was being created; leaving it alone.")
                return
            self.fchmod(fd, mode)
            self.fchown(fd, self.app_uid, self.app_gid)
            self.changed += 1

    # -- the walk -----------------------------------------------------------

    def walk(self, dir_fd: int, label: str) -> None:
        """Hand the contents of an open directory to the app user (post-order)."""
        with os.scandir(dir_fd) as entries:
            names = [entry.name for entry in entries]
        for name in names:
            path = f"{label}/{name}"
            try:
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if st.st_dev != self.data_dev or stat.S_ISLNK(st.st_mode):
                continue
            try:
                if stat.S_ISDIR(st.st_mode):
                    self.walk_dir(dir_fd, name, path, st)
                elif stat.S_ISREG(st.st_mode):
                    self.fix_file(dir_fd, name, path, st)
            except OSError as exc:
                self.warn(f"{path}: {exc.strerror}; skipped.")

    def walk_dir(self, dir_fd: int, name: str, path: str, st: os.stat_result) -> None:
        if st.st_uid == self.app_uid and not self.deep:
            # The app can change this directory right now: never act inside it.
            if self.holds_foreign_entries(dir_fd, name):
                self.warn(f"{path} belongs to the app but holds files of root; not changing them. {STOP_AND_RERUN}")
            return
        if st.st_uid != self.app_uid and not self.may_change(st.st_uid):
            return
        _hook("lstat", dir_fd, name)
        fd = self.open_dir(dir_fd, name, path, quiet=True)
        if fd is None:
            self.warn(f"{path} changed while it was being checked; skipped.")
            return
        with _closing(fd):
            _hook("open", dir_fd, name)
            fst = os.fstat(fd)
            if not self.same_entry(st, fst) or not stat.S_ISDIR(fst.st_mode):
                self.warn(f"{path} changed while it was being checked; skipped.")
                return
            owned = fst.st_uid == self.app_uid
            if (owned and not self.deep) or (not owned and not self.may_change(fst.st_uid)):
                return
            self.walk(fd, path)
            if not owned:
                self.fchown(fd, self.app_uid, self.app_gid)
                self.changed += 1

    def fix_file(self, dir_fd: int, name: str, path: str, st: os.stat_result) -> None:
        if not self.may_change(st.st_uid):
            return
        if st.st_nlink != 1:
            self._hard_linked.append(path)
            return
        _hook("lstat", dir_fd, name)
        try:
            fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
        except OSError:
            self.warn(f"{path} changed while it was being checked; skipped.")
            return
        with _closing(fd):
            _hook("open", dir_fd, name)
            fst = os.fstat(fd)
            if not self.same_entry(st, fst) or not stat.S_ISREG(fst.st_mode):
                self.warn(f"{path} changed while it was being checked; skipped.")
                return
            if fst.st_nlink != 1:
                self._hard_linked.append(path)
                return
            if not self.may_change(fst.st_uid):
                return
            self.fchown(fd, self.app_uid, self.app_gid)
            self.changed += 1

    def same_entry(self, looked_at: os.stat_result, opened: os.stat_result) -> bool:
        return (opened.st_dev, opened.st_ino) == (looked_at.st_dev, looked_at.st_ino) and (
            opened.st_dev == self.data_dev
        )

    def holds_foreign_entries(self, dir_fd: int, name: str) -> bool:
        """Whether an (app-owned) directory directly holds entries of root or the installer."""
        fd = self.open_dir(dir_fd, name, name, quiet=True)
        if fd is None:
            return False
        with _closing(fd), os.scandir(fd) as entries:
            for entry in entries:
                try:
                    uid = entry.stat(follow_symlinks=False).st_uid
                except OSError:
                    continue
                if self.may_change(uid):
                    return True
        return False

    # -- bootstrap file -------------------------------------------------------

    def move_bootstrap(self, data_fd: int, state_fd: int, data_dir: str) -> None:
        src_label = f"{data_dir}/{BOOTSTRAP_SRC}"
        dst_label = f"{data_dir}/state/{BOOTSTRAP_DST}"
        try:
            src = os.open(BOOTSTRAP_SRC, _FILE_FLAGS, dir_fd=data_fd)
        except FileNotFoundError:
            return
        except OSError:
            self.warn(f"{src_label} is not a plain file; ignoring it.")
            return
        with _closing(src):
            _hook("open", data_fd, BOOTSTRAP_SRC)
            st = os.fstat(src)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_dev != self.data_dev:
                self.warn(f"{src_label} is not a plain file; ignoring it.")
                return
            if st.st_size > MAX_BOOTSTRAP_BYTES:
                self.warn(f"{src_label} is larger than {MAX_BOOTSTRAP_BYTES} bytes; ignoring it.")
                return
            content = _read_all(src)
        try:
            dst = os.open(
                BOOTSTRAP_DST,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC,
                0o600,
                dir_fd=state_fd,
            )
        except FileExistsError:
            self.warn(f"{dst_label} already exists; leaving {src_label} where it is.")
            return
        try:
            with _closing(dst):
                _hook("open", state_fd, BOOTSTRAP_DST)
                _write_all(dst, content)
                self.fchmod(dst, 0o600)
                self.fchown(dst, self.app_uid, self.app_gid)
                os.fsync(dst)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(BOOTSTRAP_DST, dir_fd=state_fd)
            raise
        os.unlink(BOOTSTRAP_SRC, dir_fd=data_fd)
        with contextlib.suppress(OSError):
            os.fsync(state_fd)
        self.info(f"Moved {src_label} to {dst_label}")


def _read_all(fd: int) -> bytes:
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return b"".join(chunks)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -I -m specivo.ops.fix_ownership", description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--uid", type=int, default=int(os.environ.get("SPECIVO_UID", "10001")))
    parser.add_argument("--gid", type=int, default=int(os.environ.get("SPECIVO_GID", "10001")))
    parser.add_argument(
        "--all",
        action="store_true",
        help="also enter directories the app user already owns; only with every app container stopped",
    )
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("fix_ownership must run as root.", file=sys.stderr)
        return 2
    Fixer(app_uid=args.uid, app_gid=args.gid, deep=args.all).run(args.data_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
