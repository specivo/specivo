"""Where celery beat keeps its schedule state.

Beat's persistent scheduler stores its state with :mod:`shelve`, i.e. a
pickle, so the file must not live anywhere another user can write to: not the
working directory (``/app`` in the container, read-only for the app user) and
not a shared temp directory. By default it goes to a private (0700) state
directory in the app user's home, ``$XDG_STATE_HOME/specivo`` or
``~/.local/state/specivo``. When that cannot be used (a read-only root
filesystem without a writable home), beat falls back to a fresh private
directory from :func:`tempfile.mkdtemp`; the schedule then starts over on
every restart, which only means the periodic tasks run once more early.

``CELERY_BEAT_SCHEDULE_FILE`` (or ``celery beat --schedule``) sets the file
explicitly; it is then used as given.
"""

from __future__ import annotations

import functools
import logging
import os
import stat
import tempfile
from pathlib import Path

from celery.beat import PersistentScheduler  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

SCHEDULE_FILENAME = "celerybeat-schedule"


def state_dir() -> Path | None:
    """The app's private state directory, or None when there is no home."""
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "specivo"
    try:
        return Path.home() / ".local" / "state" / "specivo"
    except RuntimeError:
        return None


def default_schedule_file() -> str:
    """The configured schedule path: the env override, else the state directory."""
    override = os.environ.get("CELERY_BEAT_SCHEDULE_FILE")
    if override:
        return override
    directory = state_dir()
    if directory is None:
        return ""
    return str(directory / SCHEDULE_FILENAME)


def ensure_private_dir(path: Path) -> bool:
    """Create *path* with mode 0700 if missing; True if it is usable as private.

    Usable means: a real directory (not a symlink), owned by this process's
    user, writable, and closed to everyone else (tightened to 0700 if needed).
    """
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        return False
    if info.st_mode & 0o077:
        try:
            os.chmod(path, 0o700)
        except OSError:
            return False
    return os.access(path, os.W_OK | os.X_OK)


def prepare_schedule_file(filename: str | None) -> str:
    """Return the schedule path to use, making sure its directory is private."""
    directory = state_dir()
    default = str(directory / SCHEDULE_FILENAME) if directory is not None else ""
    if filename and filename != default:
        return filename  # set explicitly: the operator's choice
    if directory is not None and ensure_private_dir(directory):
        return default
    fallback = _fallback_dir()
    logger.warning(
        "Cannot use %s for the celery beat schedule; using %s, which does not survive a restart. "
        "Give the app user a writable home or set CELERY_BEAT_SCHEDULE_FILE.",
        directory,
        fallback,
    )
    return os.path.join(fallback, SCHEDULE_FILENAME)


@functools.cache
def _fallback_dir() -> str:
    # One per process: beat builds its scheduler more than once.
    return tempfile.mkdtemp(prefix="specivo-beat-")


class PrivateStateScheduler(PersistentScheduler):
    """PersistentScheduler that keeps its file in a private directory."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs["schedule_filename"] = prepare_schedule_file(kwargs.get("schedule_filename"))
        super().__init__(*args, **kwargs)
