"""The links file: imported texts that wait for records a later import may bring.

A placeholder whose record was not imported is written as plain text (see
:mod:`specivo.importers.core.placeholders`). When a source is exported in parts,
one project at a time say, the record often arrives with a later bundle. To link
the text then, an import remembers each such text in a small SQLite file kept
next to the bundles: which row and column it is, its source text with the
placeholders, a hash of what Specivo wrote, and the placeholders still waiting.
A later import of the same source re-expands a text once its records exist,
unless somebody has edited it since (the hash no longer matches).

The file is a convenience, not a record: losing it only means those references
stay plain text. One file belongs to one identifier-map scope (source system and
instance), which its ``meta`` table names; a file of another scope, or of a
format version this code does not know, is refused.

Everything here is synchronous: the file is small and local. A store is opened
for one import or command and holds one SQLite transaction until
:meth:`LinksStore.commit` or :meth:`LinksStore.rollback`, so the import can
commit it only after the database transaction it describes has committed.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

LINKS_FORMAT_VERSION = "1"

# How long to wait for another process holding the file, in milliseconds.
_BUSY_TIMEOUT_MS = 5000

_SCHEMA = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE pending ("
    " entity_type TEXT NOT NULL, row_id INTEGER NOT NULL, column_name TEXT NOT NULL,"
    " source_text TEXT NOT NULL, written_sha256 TEXT NOT NULL,"
    " PRIMARY KEY (entity_type, row_id, column_name))",
    "CREATE TABLE pending_ref ("
    " entity_type TEXT NOT NULL, row_id INTEGER NOT NULL, column_name TEXT NOT NULL,"
    " kind TEXT NOT NULL, ref TEXT NOT NULL,"
    " PRIMARY KEY (entity_type, row_id, column_name, kind, ref))",
    "CREATE INDEX ix_pending_ref_kind_ref ON pending_ref (kind, ref)",
)

# Characters kept as they are in the instance part of a file name.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_SLUG_MAX = 60


class LinksFileError(Exception):
    """The links file cannot be used."""


class LinksFileUnreadableError(LinksFileError):
    """The file is not a links file SQLite can read: corrupt, truncated or something else."""


class LinksFileMismatchError(LinksFileError):
    """The file belongs to another scope, or was written in an unknown format version."""


@dataclass(frozen=True, slots=True)
class PendingText:
    """One imported text with placeholders that are still waiting for their records."""

    entity_type: str
    row_id: int
    column: str
    source_text: str
    written_sha256: str
    refs: tuple[tuple[str, str], ...]

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.entity_type, self.row_id, self.column)


def text_sha256(text: str) -> str:
    """Return the hex SHA-256 of *text* as UTF-8: what a stored text is compared by."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def instance_slug(instance: str) -> str:
    """Return *instance* as a safe file-name part.

    Kept as it is when it is already safe; otherwise the unsafe runs become
    ``-`` and a short hash of the original is appended, so two instances that
    slug alike still get different files.
    """
    slug = _UNSAFE.sub("-", instance).strip("-.")[:_SLUG_MAX] or "instance"
    if slug == instance:
        return slug
    return f"{slug}-{hashlib.sha256(instance.encode('utf-8')).hexdigest()[:8]}"


def default_links_path(directory: Path, system: str, instance: str) -> Path:
    """Return the links file a scope uses in *directory*: ``specivo-links.<system>.<instance>.sqlite``."""
    return directory / f"specivo-links.{system}.{instance_slug(instance)}.sqlite"


def is_writable(path: Path) -> bool:
    """Whether *path* can be created or rewritten (SQLite also writes its journal beside it)."""
    directory = path.parent
    if not directory.is_dir() or not os.access(directory, os.W_OK | os.X_OK):
        return False
    return not path.exists() or os.access(path, os.R_OK | os.W_OK)


def _connect_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _read_meta(conn: sqlite3.Connection, path: Path) -> dict[str, str] | None:
    """Return the file's ``meta`` table, or ``None`` for an empty (new) file."""
    try:
        tables = _tables(conn)
        if not tables:
            return None
        if not {"meta", "pending", "pending_ref"} <= tables:
            raise LinksFileUnreadableError(f"{path} is an SQLite file, but not a links file")
        return {key: value for key, value in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.Error as exc:
        raise LinksFileUnreadableError(f"{path} cannot be read: {exc}") from exc


def _check_meta(meta: dict[str, str], path: Path, system: str | None, instance: str | None) -> tuple[str, str]:
    version = meta.get("format_version")
    if version != LINKS_FORMAT_VERSION:
        raise LinksFileMismatchError(
            f"{path} is a links file of format version {version!r}, which this version of Specivo does not know "
            f"(it writes version {LINKS_FORMAT_VERSION})"
        )
    scope = (meta.get("source_system", ""), meta.get("source_instance", ""))
    if system is not None and scope != (system, instance):
        raise LinksFileMismatchError(
            f"{path} belongs to the import of {scope[0]} @ {scope[1]}, not {system} @ {instance}. "
            "Give this import its own file with --links-file"
        )
    return scope


def read_scope(path: Path) -> tuple[str, str] | None:
    """Return the ``(system, instance)`` a links file belongs to, or ``None`` for an empty file.

    Raises :class:`LinksFileUnreadableError` or :class:`LinksFileMismatchError`
    (an unknown format version).
    """
    try:
        conn = _connect_read_only(path)
    except sqlite3.Error as exc:
        raise LinksFileUnreadableError(f"{path} cannot be opened: {exc}") from exc
    try:
        meta = _read_meta(conn, path)
        return None if meta is None else _check_meta(meta, path, None, None)
    finally:
        conn.close()


def check_scope(path: Path, system: str, instance: str) -> None:
    """Raise unless the links file at *path* is empty or belongs to *system* @ *instance*."""
    try:
        conn = _connect_read_only(path)
    except sqlite3.Error as exc:
        raise LinksFileUnreadableError(f"{path} cannot be opened: {exc}") from exc
    try:
        meta = _read_meta(conn, path)
        if meta is not None:
            _check_meta(meta, path, system, instance)
    finally:
        conn.close()


class LinksStore:
    """One links file, open for one import or command, inside one transaction.

    Opened writable, it holds an immediate SQLite transaction on the file
    itself until :meth:`commit` or :meth:`rollback`. Opened read-only (a dry
    run) it works on an in-memory copy, so the same code runs and the file is
    never touched.
    """

    def __init__(self, conn: sqlite3.Connection, path: Path, *, persistent: bool, created: bool) -> None:
        self._conn: sqlite3.Connection | None = conn
        self.path = path
        self.persistent = persistent
        self._created = created

    # ------------------------------------------------------------------
    # Opening
    # ------------------------------------------------------------------

    @classmethod
    def open(cls, path: Path, system: str, instance: str, *, writable: bool) -> LinksStore:
        """Open the links file at *path* for *system* @ *instance*.

        A missing file is created when *writable* (and deleted again if the
        store is rolled back or ends up empty); read-only it starts empty.
        Raises :class:`LinksFileUnreadableError`, :class:`LinksFileMismatchError`,
        or :class:`sqlite3.Error` when the file cannot be locked or written.
        """
        if not writable:
            return cls._open_copy(path, system, instance)

        created = not path.exists()
        conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            # No WAL: a single file, with no side files to be left behind or forgotten.
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("BEGIN IMMEDIATE")
            meta = _read_meta(conn, path)
            if meta is None:
                _initialise(conn, system, instance)
            else:
                _check_meta(meta, path, system, instance)
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            conn.close()
            if created:
                path.unlink(missing_ok=True)
            raise
        return cls(conn, path, persistent=True, created=created)

    @classmethod
    def _open_copy(cls, path: Path, system: str, instance: str) -> LinksStore:
        memory = sqlite3.connect(":memory:", isolation_level=None)
        if path.exists():
            try:
                source = _connect_read_only(path)
            except sqlite3.Error as exc:
                memory.close()
                raise LinksFileUnreadableError(f"{path} cannot be opened: {exc}") from exc
            try:
                meta = _read_meta(source, path)
                if meta is not None:
                    _check_meta(meta, path, system, instance)
                    source.backup(memory)
            except BaseException:
                memory.close()
                raise
            finally:
                source.close()
        if not _tables(memory):
            _initialise(memory, system, instance)
        memory.execute("BEGIN")
        return cls(memory, path, persistent=False, created=False)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise LinksFileError("The links file is closed")
        return self._conn

    def count(self) -> int:
        """Return how many texts are waiting."""
        return int(self.conn.execute("SELECT count(*) FROM pending").fetchone()[0])

    def counts_by_kind(self) -> dict[str, int]:
        """Return how many distinct records each kind of placeholder waits for."""
        rows = self.conn.execute(
            "SELECT kind, count(DISTINCT ref) FROM pending_ref GROUP BY kind ORDER BY kind"
        ).fetchall()
        return {kind: int(count) for kind, count in rows}

    def refs(self) -> list[tuple[str, str]]:
        """Return every ``(kind, ref)`` some text waits for, once each."""
        return [(kind, ref) for kind, ref in self.conn.execute("SELECT DISTINCT kind, ref FROM pending_ref")]

    def texts_naming(self, keys: Iterable[tuple[str, str]]) -> list[PendingText]:
        """Return the waiting texts that name any of *keys*, ``(kind, ref)`` pairs."""
        conn = self.conn
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS wanted (kind TEXT NOT NULL, ref TEXT NOT NULL)")
        conn.execute("DELETE FROM temp.wanted")
        conn.executemany("INSERT INTO temp.wanted (kind, ref) VALUES (?, ?)", list(keys))
        rows = conn.execute(
            "SELECT p.entity_type, p.row_id, p.column_name, p.source_text, p.written_sha256 FROM pending p "
            "WHERE EXISTS (SELECT 1 FROM pending_ref r JOIN temp.wanted w ON w.kind = r.kind AND w.ref = r.ref "
            "WHERE r.entity_type = p.entity_type AND r.row_id = p.row_id AND r.column_name = p.column_name) "
            "ORDER BY p.entity_type, p.column_name, p.row_id"
        ).fetchall()
        conn.execute("DELETE FROM temp.wanted")
        return [self._with_refs(*row) for row in rows]

    def get(self, entity_type: str, row_id: int, column: str) -> PendingText | None:
        row = self.conn.execute(
            "SELECT entity_type, row_id, column_name, source_text, written_sha256 FROM pending "
            "WHERE entity_type = ? AND row_id = ? AND column_name = ?",
            (entity_type, row_id, column),
        ).fetchone()
        return self._with_refs(*row) if row else None

    def _with_refs(self, entity_type: str, row_id: int, column: str, source_text: str, sha: str) -> PendingText:
        refs = self.conn.execute(
            "SELECT kind, ref FROM pending_ref WHERE entity_type = ? AND row_id = ? AND column_name = ? "
            "ORDER BY kind, ref",
            (entity_type, row_id, column),
        ).fetchall()
        return PendingText(entity_type, int(row_id), column, source_text, sha, tuple((k, r) for k, r in refs))

    # ------------------------------------------------------------------
    # Writes (inside the open transaction)
    # ------------------------------------------------------------------

    def put(self, text: PendingText) -> None:
        """Store *text*, replacing what was stored for the same row and column."""
        self.delete(*text.key)
        self.conn.execute(
            "INSERT INTO pending (entity_type, row_id, column_name, source_text, written_sha256) "
            "VALUES (?, ?, ?, ?, ?)",
            (text.entity_type, text.row_id, text.column, text.source_text, text.written_sha256),
        )
        self.conn.executemany(
            "INSERT OR IGNORE INTO pending_ref (entity_type, row_id, column_name, kind, ref) VALUES (?, ?, ?, ?, ?)",
            [(text.entity_type, text.row_id, text.column, kind, ref) for kind, ref in text.refs],
        )

    def delete(self, entity_type: str, row_id: int, column: str) -> None:
        """Forget the text stored for one row and column, if any."""
        for table in ("pending_ref", "pending"):
            self.conn.execute(
                f"DELETE FROM {table} WHERE entity_type = ? AND row_id = ? AND column_name = ?",  # noqa: S608
                (entity_type, row_id, column),
            )

    # ------------------------------------------------------------------
    # Ending
    # ------------------------------------------------------------------

    def commit(self) -> bool:
        """Make the changes permanent and close; return whether the file still exists.

        A file left with nothing waiting is deleted instead. A read-only store
        is simply closed. Raises :class:`sqlite3.Error` if SQLite cannot commit.
        """
        if not self.persistent:
            self._close()
            return self.path.exists()
        if self.count() == 0:
            self._close(rollback=True)
            self.path.unlink(missing_ok=True)
            return False
        self.conn.execute("COMMIT")
        self._close()
        return True

    def rollback(self) -> None:
        """Discard the changes and close. A file this store created is removed again."""
        if self._conn is None:
            return
        self._close(rollback=True)
        if self.persistent and self._created:
            self.path.unlink(missing_ok=True)

    def _close(self, *, rollback: bool = False) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            if rollback and conn.in_transaction:
                conn.execute("ROLLBACK")
        finally:
            conn.close()


def _initialise(conn: sqlite3.Connection, system: str, instance: str) -> None:
    for statement in _SCHEMA:
        conn.execute(statement)
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [("format_version", LINKS_FORMAT_VERSION), ("source_system", system), ("source_instance", instance)],
    )
