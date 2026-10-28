"""Open a bundle, from a directory or a ``.zip``, without trusting its layout.

Everything downstream reads a bundle as a directory. A zip is therefore
unpacked into a private temporary directory first, after every entry has been
checked: an entry whose name climbs out of the bundle (``../``, an absolute
path, a drive letter), a symbolic link, a device or an encrypted entry makes
the whole archive refused before a byte is written. A directory bundle gets the
same treatment for links: a symbolic link anywhere in it is refused rather than
followed, because a bundle comes from outside and a link would let it point the
import at any file the server can read.

Unpacking is bounded by :class:`~specivo.importers.bundle.limits.BundleLimits`:
total size, number of entries, size of one entry and how far one entry may
expand, all counted on the bytes actually written, so an archive that lies in
its headers is caught as well. Unpacking costs disk space, but the attachment loader copies a file from a
path, and the readers seek within the JSON-lines files, which a compressed zip
member cannot do cheaply.
"""

from __future__ import annotations

import stat
import tempfile
import zipfile
from pathlib import Path

from specivo.importers.bundle.format import FILES_DIR, KNOWN_FILES, SHA256_RE
from specivo.importers.bundle.limits import RATIO_CHECKED_FROM, BundleLimits, format_size
from specivo.importers.bundle.report import ProblemCode, ValidationReport

_COPY_CHUNK = 1024 * 1024


class BundleContainer:
    """An opened bundle: the directory holding its files.

    For a zip that is a temporary directory, removed by :meth:`close`.
    """

    def __init__(self, root: Path, temporary: tempfile.TemporaryDirectory[str] | None = None) -> None:
        self.root = root
        self._temporary = temporary

    def close(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None


def open_bundle(path: Path, report: ValidationReport, limits: BundleLimits | None = None) -> BundleContainer | None:
    """Open the bundle at *path*, or return ``None`` with the reason in *report*."""
    if path.is_dir():
        return BundleContainer(path) if _check_directory(path, report) else None
    if path.is_file() and zipfile.is_zipfile(path):
        return _unpack_zip(path, report, limits or BundleLimits())
    report.error(ProblemCode.UNREADABLE, path.name, "Not a bundle: expected a directory or a .zip archive")
    return None


# --------------------------------------------------------------------------
# Directories
# --------------------------------------------------------------------------


def _check_directory(root: Path, report: ValidationReport) -> bool:
    """Check a directory bundle holds no links. Unknown entries are warned about and ignored."""
    safe = True
    for entry in sorted(root.iterdir()):
        name = entry.name
        if entry.is_symlink():
            report.error(ProblemCode.SYMLINK, name, "Symbolic links are not allowed in a bundle")
            safe = False
        elif name == FILES_DIR and entry.is_dir():
            safe = _check_files_dir(entry, report) and safe
        elif name not in KNOWN_FILES:
            report.warning(ProblemCode.UNKNOWN_FILE, name, "Not part of the bundle format; ignored")
    return safe


def _check_files_dir(files_dir: Path, report: ValidationReport) -> bool:
    safe = True
    for entry in sorted(files_dir.iterdir()):
        name = f"{FILES_DIR}/{entry.name}"
        if entry.is_symlink():
            report.error(ProblemCode.SYMLINK, name, "Symbolic links are not allowed in a bundle")
            safe = False
        elif not entry.is_file() or not SHA256_RE.match(entry.name):
            report.warning(ProblemCode.UNKNOWN_FILE, name, "Not a stored file (files/<sha256>); ignored")
    return safe


# --------------------------------------------------------------------------
# Zip archives
# --------------------------------------------------------------------------


class _LimitExceededError(Exception):
    def __init__(self, entry: str, message: str) -> None:
        super().__init__(message)
        self.entry = entry


def _unpack_zip(path: Path, report: ValidationReport, limits: BundleLimits) -> BundleContainer | None:
    try:
        with zipfile.ZipFile(path) as archive:
            wanted = _check_zip_entries(archive, report)
            if wanted is None:
                return None
            if not _check_declared_sizes(archive, wanted, report, limits):
                return None

            temporary = tempfile.TemporaryDirectory(prefix="specivo-bundle-")
            root = Path(temporary.name)
            try:
                total = 0
                for info, parts in wanted:
                    target = root.joinpath(*parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    total += _copy_entry(archive, info, target, total, limits)
            except _LimitExceededError as exc:
                temporary.cleanup()
                report.error(ProblemCode.TOO_LARGE, exc.entry, str(exc))
                return None
            except BaseException:
                temporary.cleanup()
                raise
            return BundleContainer(root, temporary)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, RuntimeError) as exc:
        report.error(ProblemCode.UNREADABLE, path.name, f"The archive could not be read: {exc}")
        return None


def _check_declared_sizes(
    archive: zipfile.ZipFile,
    wanted: list[tuple[zipfile.ZipInfo, list[str]]],
    report: ValidationReport,
    limits: BundleLimits,
) -> bool:
    """Refuse an archive whose own headers already exceed the limits, before unpacking anything.

    The headers are the archive's claim, not a measurement: :func:`_copy_entry`
    counts the bytes again as they are written.
    """
    name = Path(archive.filename or "bundle.zip").name
    if len(archive.infolist()) > limits.max_entries:
        report.error(
            ProblemCode.TOO_LARGE,
            name,
            f"The archive holds {len(archive.infolist())} entries; the limit is {limits.max_entries} (--max-entries)",
        )
        return False
    declared = sum(info.file_size for info, _ in wanted)
    if declared > limits.max_unpacked_bytes:
        report.error(
            ProblemCode.TOO_LARGE,
            name,
            f"The archive unpacks to {declared} bytes; the limit is "
            f"{format_size(limits.max_unpacked_bytes)} (--max-unpacked-size)",
        )
        return False
    for info, _ in wanted:
        problem = _entry_problem(info.file_size, info.compress_size, limits)
        if problem:
            report.error(ProblemCode.TOO_LARGE, info.filename, problem)
            return False
    return True


def _entry_problem(size: int, compressed: int, limits: BundleLimits) -> str | None:
    """Return why an entry of *size* bytes, stored in *compressed*, is refused, if it is."""
    if size > limits.max_entry_bytes:
        return f"Entry unpacks to more than {format_size(limits.max_entry_bytes)} (--max-entry-size)"
    if size > RATIO_CHECKED_FROM and size > max(compressed, 1) * limits.max_compression_ratio:
        return (
            f"Entry expands more than {limits.max_compression_ratio} times its stored size, "
            "which is what a zip bomb does (--max-compression-ratio)"
        )
    return None


def _copy_entry(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path, unpacked_so_far: int, limits: BundleLimits
) -> int:
    """Unpack one entry to *target*, counting the bytes; return how many were written."""
    written = 0
    with archive.open(info) as source, target.open("wb") as sink:
        while chunk := source.read(_COPY_CHUNK):
            written += len(chunk)
            problem = _entry_problem(written, info.compress_size, limits)
            if problem:
                raise _LimitExceededError(info.filename, problem)
            if unpacked_so_far + written > limits.max_unpacked_bytes:
                raise _LimitExceededError(
                    info.filename,
                    f"The archive unpacks to more than {format_size(limits.max_unpacked_bytes)} (--max-unpacked-size)",
                )
            sink.write(chunk)
    return written


def _check_zip_entries(
    archive: zipfile.ZipFile, report: ValidationReport
) -> list[tuple[zipfile.ZipInfo, list[str]]] | None:
    """Return the entries to unpack, or ``None`` if any entry is unsafe."""
    safe = True
    seen: set[str] = set()
    wanted: list[tuple[zipfile.ZipInfo, list[str]]] = []

    for info in archive.infolist():
        name = info.filename
        parts = _safe_parts(name)
        if parts is None:
            report.error(ProblemCode.UNSAFE_ENTRY, name, "Entry name leaves the bundle (absolute path or '..')")
            safe = False
            continue

        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            report.error(ProblemCode.SYMLINK, name, "Symbolic links are not allowed in a bundle")
            safe = False
            continue
        # Archivers that record no file type (Python's own, for one) leave
        # the type bits zero; only a type that is set and not plain counts.
        kind = stat.S_IFMT(mode)
        if kind and kind not in (stat.S_IFREG, stat.S_IFDIR):
            report.error(ProblemCode.UNSAFE_ENTRY, name, "Only regular files and directories are allowed")
            safe = False
            continue
        if info.flag_bits & 0x1:
            report.error(ProblemCode.UNSAFE_ENTRY, name, "Encrypted entries are not supported")
            safe = False
            continue

        key = "/".join(parts)
        if key in seen:
            report.error(ProblemCode.UNSAFE_ENTRY, name, "The archive holds this entry twice")
            safe = False
            continue
        seen.add(key)

        if info.is_dir():
            continue
        if len(parts) == 1 and key in KNOWN_FILES:
            wanted.append((info, parts))
        elif len(parts) == 2 and parts[0] == FILES_DIR and SHA256_RE.match(parts[1]):
            wanted.append((info, parts))
        else:
            report.warning(ProblemCode.UNKNOWN_FILE, name, "Not part of the bundle format; ignored")

    return wanted if safe else None


def _safe_parts(name: str) -> list[str] | None:
    """Split a zip entry name into path parts, or ``None`` if it is not a plain relative path."""
    if not name or "\\" in name or name.startswith("/") or "\x00" in name:
        return None
    parts = name.split("/")
    if parts[-1] == "":
        # A directory entry ends with a slash.
        parts = parts[:-1]
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    if ":" in parts[0]:
        # A drive letter, which Windows tools resolve outside the target.
        return None
    return parts
