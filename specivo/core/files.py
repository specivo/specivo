"""Opening files that came from outside: a regular file, never a link, pipe or device."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO

# Absent on Windows, which has neither FIFOs nor this kind of link.
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)


def open_regular(path: Path) -> BinaryIO:
    """Open *path* for reading in binary, provided it is a regular file and not a link.

    Opened without blocking, so a pipe put in place of the file since it was
    checked cannot make the open wait for a writer; then checked with
    ``fstat`` on the open descriptor, which no later swap can change, and only
    then switched back to blocking reads. Anything else raises :class:`OSError`.
    """
    fd = os.open(path, _OPEN_FLAGS)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"Not a regular file: {path.name}")
        os.set_blocking(fd, True)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
