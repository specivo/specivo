"""The links file: an SQLite file of imported texts waiting for their records."""

from __future__ import annotations

import os
import sqlite3

import pytest

from specivo.importers.core.links_file import (
    LinksFileMismatchError,
    LinksFileUnreadableError,
    LinksStore,
    PendingText,
    check_scope,
    default_links_path,
    instance_slug,
    is_writable,
    read_scope,
    text_sha256,
)

pytestmark = pytest.mark.unit

SYSTEM, INSTANCE = "sample", "tracker.example.com"


def _text(row_id: int = 1, *refs: tuple[str, str], entity: str = "issue", column: str = "description") -> PendingText:
    return PendingText(
        entity, row_id, column, "See {{issue:web-1}}.", text_sha256("See web-1."), refs or (("issue", "web-1"),)
    )


def _store(path, *, writable: bool = True) -> LinksStore:
    return LinksStore.open(path, SYSTEM, INSTANCE, writable=writable)


class TestNames:
    def test_a_safe_instance_is_kept(self):
        assert instance_slug("tracker.example.com") == "tracker.example.com"

    def test_an_unsafe_instance_is_slugged_with_a_hash(self):
        slug = instance_slug("postgresql://redmine:***@db.internal/redmine")
        assert slug.startswith("postgresql-redmine-db.internal-redmine-")
        assert len(slug.rsplit("-", 1)[1]) == 8

    def test_instances_that_slug_alike_get_different_names(self):
        assert instance_slug("a/b") != instance_slug("a:b")

    def test_the_default_path(self, tmp_path):
        assert default_links_path(tmp_path, SYSTEM, INSTANCE) == (
            tmp_path / "specivo-links.sample.tracker.example.com.sqlite"
        )


class TestStore:
    def test_a_new_file_holds_its_scope_and_the_texts(self, tmp_path):
        path = tmp_path / "links.sqlite"
        store = _store(path)
        store.put(_text(1, ("issue", "web-1"), ("wiki", "w-home")))
        assert store.commit() is True

        assert read_scope(path) == (SYSTEM, INSTANCE)
        again = _store(path, writable=False)
        assert again.count() == 1
        assert again.counts_by_kind() == {"issue": 1, "wiki": 1}
        assert again.get("issue", 1, "description") == _text(1, ("issue", "web-1"), ("wiki", "w-home"))
        again.rollback()

    def test_texts_are_found_by_the_records_they_name(self, tmp_path):
        store = _store(tmp_path / "links.sqlite")
        store.put(_text(1, ("issue", "web-1")))
        store.put(_text(2, ("issue", "web-2"), ("wiki", "w-home")))
        store.put(_text(3, ("user", "u-1")))

        found = store.texts_naming([("wiki", "w-home"), ("issue", "web-1")])

        assert [text.row_id for text in found] == [1, 2]
        assert sorted(store.refs()) == [("issue", "web-1"), ("issue", "web-2"), ("user", "u-1"), ("wiki", "w-home")]
        store.rollback()

    def test_put_replaces_a_texts_row_and_refs(self, tmp_path):
        store = _store(tmp_path / "links.sqlite")
        store.put(_text(1, ("issue", "web-1"), ("issue", "web-2")))
        store.put(_text(1, ("issue", "web-2")))

        assert store.get("issue", 1, "description").refs == (("issue", "web-2"),)
        assert store.texts_naming([("issue", "web-1")]) == []
        store.rollback()

    def test_a_file_with_nothing_waiting_is_deleted(self, tmp_path):
        path = tmp_path / "links.sqlite"
        store = _store(path)
        store.put(_text(1))
        store.commit()

        store = _store(path)
        store.delete("issue", 1, "description")
        assert store.commit() is False
        assert not path.exists()

    def test_nothing_to_write_creates_no_file(self, tmp_path):
        path = tmp_path / "links.sqlite"
        assert _store(path).commit() is False
        assert not path.exists()

    def test_a_rollback_leaves_the_file_as_it_was(self, tmp_path):
        """A crash before the commit: nothing the transaction did reaches the file."""
        path = tmp_path / "links.sqlite"
        store = _store(path)
        store.put(_text(1))
        store.commit()
        before = path.read_bytes()

        store = _store(path)
        store.delete("issue", 1, "description")
        store.put(_text(2))
        store.rollback()

        assert path.read_bytes() == before
        assert not (tmp_path / "links.sqlite-journal").exists()

    def test_a_rollback_removes_a_file_it_created(self, tmp_path):
        path = tmp_path / "links.sqlite"
        store = _store(path)
        store.put(_text(1))
        store.rollback()
        assert not path.exists()

    def test_an_uncommitted_store_does_not_reach_the_file(self, tmp_path):
        """The transaction is only made permanent by commit: a process that dies first leaves no change."""
        path = tmp_path / "links.sqlite"
        store = _store(path)
        store.put(_text(1))
        store.commit()

        store = _store(path)
        store.put(_text(2))
        reader = sqlite3.connect(path, timeout=0.1)
        assert reader.execute("SELECT count(*) FROM pending").fetchone()[0] == 1
        reader.close()
        store.rollback()

    def test_a_read_only_store_works_on_a_copy(self, tmp_path):
        path = tmp_path / "links.sqlite"
        store = _store(path)
        store.put(_text(1))
        store.commit()
        before = path.read_bytes()

        dry = _store(path, writable=False)
        dry.delete("issue", 1, "description")
        dry.put(_text(5))
        assert dry.count() == 1
        dry.commit()

        assert path.read_bytes() == before

    def test_a_read_only_store_of_a_missing_file_starts_empty(self, tmp_path):
        dry = _store(tmp_path / "missing.sqlite", writable=False)
        assert dry.count() == 0
        dry.commit()
        assert not (tmp_path / "missing.sqlite").exists()


class TestRefusals:
    def _written(self, path, system=SYSTEM, instance=INSTANCE):
        store = LinksStore.open(path, system, instance, writable=True)
        store.put(_text(1))
        store.commit()
        return path

    def test_a_file_of_another_scope_is_refused(self, tmp_path):
        path = self._written(tmp_path / "links.sqlite", instance="other.example.com")
        with pytest.raises(LinksFileMismatchError, match=r"belongs to the import of sample @ other\.example\.com"):
            _store(path)
        with pytest.raises(LinksFileMismatchError, match="--links-file"):
            check_scope(path, SYSTEM, INSTANCE)
        with pytest.raises(LinksFileMismatchError):
            _store(path, writable=False)

    def test_an_unknown_format_version_is_refused(self, tmp_path):
        path = self._written(tmp_path / "links.sqlite")
        conn = sqlite3.connect(path)
        conn.execute("UPDATE meta SET value = '9' WHERE key = 'format_version'")
        conn.commit()
        conn.close()
        with pytest.raises(LinksFileMismatchError, match="format version '9'"):
            read_scope(path)

    def test_a_corrupt_file_is_unreadable(self, tmp_path):
        path = tmp_path / "links.sqlite"
        path.write_bytes(b"this is not an SQLite database, only some bytes" * 50)
        with pytest.raises(LinksFileUnreadableError):
            check_scope(path, SYSTEM, INSTANCE)
        with pytest.raises(LinksFileUnreadableError):
            _store(path, writable=False)

    def test_another_sqlite_file_is_not_a_links_file(self, tmp_path):
        path = tmp_path / "other.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE notes (body TEXT)")
        conn.commit()
        conn.close()
        with pytest.raises(LinksFileUnreadableError, match="not a links file"):
            read_scope(path)

    @pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permissions")
    def test_a_read_only_directory_is_not_writable(self, tmp_path):
        folder = tmp_path / "ro"
        folder.mkdir()
        folder.chmod(0o555)
        try:
            assert is_writable(folder / "links.sqlite") is False
        finally:
            folder.chmod(0o755)
        assert is_writable(folder / "links.sqlite") is True
        assert is_writable(tmp_path / "missing" / "links.sqlite") is False
