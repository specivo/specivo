"""The links file: an SQLite file of imported texts waiting for their records."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from dataclasses import replace

import pytest

from specivo.importers.core import links_file
from specivo.importers.core.links_file import (
    LinksFileMismatchError,
    LinksFileUnreadableError,
    LinksStore,
    PendingParent,
    PendingRelation,
    PendingText,
    check_scope,
    default_links_path,
    instance_slug,
    is_writable,
    links_signing_key,
    read_scope,
    text_sha256,
)

pytestmark = pytest.mark.unit

SYSTEM, INSTANCE = "sample", "tracker.example.com"


def _text(row_id: int = 1, *refs: tuple[str, str], entity: str = "issue", column: str = "description") -> PendingText:
    return PendingText(
        entity, row_id, column, "See {{issue:web-1}}.", text_sha256("See web-1."), refs or (("issue", "web-1"),)
    )


def _relation(source_ref: str = "r-1", to_ref: str = "b-1") -> PendingRelation:
    return PendingRelation(source_ref, "a-1", to_ref, "blocks", None)


def _parent(child_ref: str = "b-2", parent_ref: str = "a-2") -> PendingParent:
    return PendingParent(child_ref, parent_ref)


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

    def test_a_file_of_an_earlier_format_version_is_refused_with_what_to_do(self, tmp_path):
        """Version 1 kept texts only; it is refused, never read or upgraded."""
        path = tmp_path / "links.sqlite"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "CREATE TABLE pending (entity_type TEXT, row_id INTEGER, column_name TEXT, source_text TEXT,"
            " written_sha256 TEXT, signature TEXT);"
            "CREATE TABLE pending_ref (entity_type TEXT, row_id INTEGER, column_name TEXT, kind TEXT, ref TEXT);"
            f"INSERT INTO meta VALUES ('format_version', '1'), ('source_system', '{SYSTEM}'),"
            f" ('source_instance', '{INSTANCE}');"
        )
        conn.close()
        for call in (lambda: read_scope(path), lambda: _store(path), lambda: check_scope(path, SYSTEM, INSTANCE)):
            with pytest.raises(LinksFileMismatchError, match=r"format version '1'.*earlier version.*[Dd]elete it"):
                call()

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


KEY_A = links_signing_key("the-secret-key-of-this-installation-0123456789")
KEY_B = links_signing_key("the-secret-key-of-another-installation-987654")


class TestUntrustedFile:
    """A links file comes from wherever the bundles do: nothing in it is trusted until checked."""

    def _signed(self, path, key=KEY_A) -> PendingText:
        store = LinksStore.open(path, SYSTEM, INSTANCE, writable=True, key=key)
        store.put(_text(1))
        store.commit()
        reader = LinksStore.open(path, SYSTEM, INSTANCE, writable=False, key=KEY_A)
        (text,) = reader.texts_naming([("issue", "web-1")])
        trusted = reader.is_trusted(text)
        reader.rollback()
        return text, trusted

    def test_a_row_this_installation_wrote_is_trusted(self, tmp_path):
        _, trusted = self._signed(tmp_path / "links.sqlite")
        assert trusted is True

    def test_a_row_signed_with_another_key_is_not(self, tmp_path):
        _, trusted = self._signed(tmp_path / "links.sqlite", key=KEY_B)
        assert trusted is False

    def test_an_unsigned_row_is_not(self, tmp_path):
        path = tmp_path / "links.sqlite"
        self._signed(path)
        conn = sqlite3.connect(path)
        conn.execute("UPDATE pending SET signature = ''")
        conn.commit()
        conn.close()
        reader = LinksStore.open(path, SYSTEM, INSTANCE, writable=False, key=KEY_A)
        (text,) = reader.texts_naming([("issue", "web-1")])
        assert reader.is_trusted(text) is False
        reader.rollback()

    def test_the_signature_covers_the_scope(self, tmp_path):
        """A row signed for one scope cannot be copied into another scope's file."""
        mine = tmp_path / "mine.sqlite"
        self._signed(mine)
        other = tmp_path / "other.sqlite"
        store = LinksStore.open(other, SYSTEM, "other.example.com", writable=True, key=KEY_A)
        store.put(_text(9))
        store.commit()
        conn = sqlite3.connect(other)
        signature = conn.execute("SELECT signature FROM pending").fetchone()[0]
        conn.close()
        conn = sqlite3.connect(mine)
        conn.execute("UPDATE pending SET row_id = 9, signature = ?", (signature,))
        conn.execute("UPDATE pending_ref SET row_id = 9")
        conn.commit()
        conn.close()
        reader = LinksStore.open(mine, SYSTEM, INSTANCE, writable=False, key=KEY_A)
        (text,) = reader.texts_naming([("issue", "web-1")])
        assert reader.is_trusted(text) is False
        reader.rollback()

    @pytest.mark.parametrize(
        ("column", "value"),
        [
            ("column_name", "subject"),
            ("entity_type", "project"),
            ("row_id", "twelve"),
            ("row_id", 1.5),
            ("row_id", -4),
            ("written_sha256", "not-a-hash"),
        ],
    )
    def test_a_malformed_row_is_not_trusted_even_when_signed(self, tmp_path, column, value):
        path = tmp_path / "links.sqlite"
        store = LinksStore.open(path, SYSTEM, INSTANCE, writable=True, key=KEY_A)
        store.put(_text(1))
        store.commit()
        conn = sqlite3.connect(path)
        conn.execute(f"UPDATE pending SET {column} = ?", (value,))  # noqa: S608
        if column != "written_sha256":
            conn.execute(f"UPDATE pending_ref SET {column} = ?", (value,))  # noqa: S608
        conn.commit()
        conn.close()
        reader = LinksStore.open(path, SYSTEM, INSTANCE, writable=False, key=KEY_A)
        (text,) = reader.texts_naming([("issue", "web-1")])
        # Re-signing the tampered row with the right key does not make it acceptable.
        assert reader.is_trusted(text) is False
        assert reader.is_trusted(replace(text, signature=reader.sign(text))) is False
        reader.rollback()

    def test_a_symlinked_file_is_refused(self, tmp_path):
        real = tmp_path / "real.sqlite"
        store = LinksStore.open(real, SYSTEM, INSTANCE, writable=True, key=KEY_A)
        store.put(_text(1))
        store.commit()
        before = real.read_bytes()
        link = tmp_path / "links.sqlite"
        link.symlink_to(real)

        for attempt in (
            lambda: check_scope(link, SYSTEM, INSTANCE),
            lambda: read_scope(link),
            lambda: LinksStore.open(link, SYSTEM, INSTANCE, writable=True, key=KEY_A),
            lambda: LinksStore.open(link, SYSTEM, INSTANCE, writable=False, key=KEY_A),
        ):
            with pytest.raises(LinksFileUnreadableError, match="symbolic link"):
                attempt()
        assert real.read_bytes() == before

    def test_a_dangling_symlink_is_refused_and_nothing_is_created_through_it(self, tmp_path):
        target = tmp_path / "elsewhere.sqlite"
        link = tmp_path / "links.sqlite"
        link.symlink_to(target)
        with pytest.raises(LinksFileUnreadableError, match="symbolic link"):
            LinksStore.open(link, SYSTEM, INSTANCE, writable=True, key=KEY_A)
        assert not target.exists()


class TestOpeningRaces:
    """The check that nothing (or no link) is there and the open are two steps: what comes between is refused."""

    def _attacker_file(self, tmp_path) -> tuple:
        target = tmp_path / "attacker.sqlite"
        store = LinksStore.open(target, SYSTEM, INSTANCE, writable=True, key=KEY_A)
        store.put(_text(7))
        store.commit()
        return target, target.read_bytes()

    def test_a_new_file_is_private_to_its_owner(self, tmp_path):
        path = tmp_path / "links.sqlite"
        store = LinksStore.open(path, SYSTEM, INSTANCE, writable=True, key=KEY_A)
        store.put(_text(1))
        store.commit()
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600

    @pytest.mark.parametrize("target_exists", [False, True])
    def test_a_symlink_appearing_after_the_existence_check_is_refused(self, tmp_path, monkeypatch, target_exists):
        if target_exists:
            target, before = self._attacker_file(tmp_path)
        else:
            target, before = tmp_path / "elsewhere.sqlite", None
        link = tmp_path / "links.sqlite"
        link.symlink_to(target)
        # The existence check ran while nothing was there yet.
        monkeypatch.setattr(links_file, "_refuse_symlink", lambda path: None)

        with pytest.raises(LinksFileUnreadableError, match="symbolic link"):
            LinksStore.open(link, SYSTEM, INSTANCE, writable=True, key=KEY_A)

        assert link.is_symlink()
        if target_exists:
            assert target.read_bytes() == before
        else:
            assert not target.exists()

    def test_an_existing_file_turned_into_a_symlink_is_refused(self, tmp_path, monkeypatch):
        target, before = self._attacker_file(tmp_path)
        link = tmp_path / "links.sqlite"
        link.symlink_to(target)
        # The check saw a regular file there.
        monkeypatch.setattr(links_file, "_refuse_symlink", lambda path: os.lstat(target))

        for writable in (True, False):
            with pytest.raises(LinksFileUnreadableError, match="symbolic link"):
                LinksStore.open(link, SYSTEM, INSTANCE, writable=writable, key=KEY_A)
        assert target.read_bytes() == before

    @pytest.mark.parametrize("existing", [False, True])
    @pytest.mark.parametrize("dangling", [False, True])
    def test_a_symlink_swapped_in_before_sqlite_opens_the_file_is_refused(
        self, tmp_path, monkeypatch, existing, dangling
    ):
        """The window between the no-follow open and SQLite's own open: SQLite follows links."""
        path = tmp_path / "links.sqlite"
        if existing:
            store = LinksStore.open(path, SYSTEM, INSTANCE, writable=True, key=KEY_A)
            store.put(_text(1))
            store.commit()
        if dangling:
            target, before = tmp_path / "elsewhere.sqlite", None
        else:
            target, before = self._attacker_file(tmp_path)
        real_connect = sqlite3.connect

        def swap_then_connect(*args, **kwargs):
            path.rename(tmp_path / "moved-away.sqlite")
            path.symlink_to(target)
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(links_file.sqlite3, "connect", swap_then_connect)
        with pytest.raises(LinksFileUnreadableError):
            LinksStore.open(path, SYSTEM, INSTANCE, writable=True, key=KEY_A)
        monkeypatch.setattr(links_file.sqlite3, "connect", real_connect)

        if dangling:
            assert not target.exists()
        else:
            assert target.read_bytes() == before

    def test_a_file_created_by_another_process_meanwhile_fails_cleanly(self, tmp_path, monkeypatch):
        path = tmp_path / "links.sqlite"
        store = LinksStore.open(path, SYSTEM, INSTANCE, writable=True, key=KEY_A)
        store.put(_text(1))
        store.commit()
        before = path.read_bytes()
        # The existence check ran before the other process created the file.
        monkeypatch.setattr(links_file, "_refuse_symlink", lambda path: None)

        with pytest.raises(LinksFileUnreadableError, match="created by something else"):
            LinksStore.open(path, SYSTEM, INSTANCE, writable=True, key=KEY_A)
        assert path.read_bytes() == before

    def test_a_planted_journal_link_is_refused(self, tmp_path):
        path = tmp_path / "links.sqlite"
        (tmp_path / "links.sqlite-journal").symlink_to(tmp_path / "victim.txt")
        with pytest.raises(LinksFileUnreadableError, match="-journal"):
            LinksStore.open(path, SYSTEM, INSTANCE, writable=True, key=KEY_A)
        assert not (tmp_path / "victim.txt").exists()
        assert not path.exists()


class TestSpecialFiles:
    def test_a_fifo_at_the_links_path_is_refused_at_once(self, tmp_path):
        """A planted FIFO must not hang the import: an open without O_NONBLOCK waits for a writer forever."""
        path = tmp_path / "links.sqlite"
        os.mkfifo(path)
        attempts = (
            lambda: check_scope(path, SYSTEM, INSTANCE),
            lambda: read_scope(path),
            lambda: LinksStore.open(path, SYSTEM, INSTANCE, writable=False, key=KEY_A),
            lambda: LinksStore.open(path, SYSTEM, INSTANCE, writable=True, key=KEY_A),
        )
        for attempt in attempts:
            outcome: list[BaseException | None] = []

            def run(attempt=attempt, outcome=outcome) -> None:
                try:
                    attempt()
                    outcome.append(None)
                except BaseException as exc:  # noqa: BLE001 - handed to the test thread
                    outcome.append(exc)

            # A daemon thread: if the open hangs, the test fails instead of hanging with it.
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            worker.join(timeout=5)
            assert outcome, "opening a FIFO hung"
            assert isinstance(outcome[0], LinksFileUnreadableError)
            assert "not a regular file" in str(outcome[0])
        assert stat.S_ISFIFO(os.lstat(path).st_mode)

    @pytest.mark.parametrize("honoured", [False, True])
    def test_sqlite_is_asked_not_to_follow_links_when_it_can(self, tmp_path, monkeypatch, honoured):
        monkeypatch.setattr(links_file, "sqlite_honours_nofollow", lambda: honoured)
        seen: list[str] = []
        real_connect = sqlite3.connect

        def recording(database, *args, **kwargs):
            seen.append(str(database))
            return real_connect(database, *args, **kwargs)

        monkeypatch.setattr(links_file.sqlite3, "connect", recording)
        store = LinksStore.open(tmp_path / "links.sqlite", SYSTEM, INSTANCE, writable=True, key=KEY_A)
        store.rollback()
        assert ("nofollow=1" in seen[0]) is honoured

    def test_the_probe_answers_for_this_sqlite(self):
        """Whatever this build does, the answer is a plain bool and is worked out once."""
        assert links_file.sqlite_honours_nofollow() in (True, False)
        assert links_file.sqlite_honours_nofollow.cache_info().currsize == 1


class TestWaitingLinks:
    """Relations and parents waiting for an issue of a bundle not loaded yet."""

    def test_relations_and_parents_are_kept_and_read_back(self, tmp_path):
        path = tmp_path / "links.sqlite"
        store = _store(path)
        store.put_relation(_relation())
        store.put_relation(PendingRelation("r-2", "a-1", "b-9", "precedes", 3))
        store.put_parent(_parent())
        assert store.commit() is True

        again = _store(path, writable=False)
        assert again.count() == 3
        assert again.waiting_links() == {"parent": 1, "relation": 2}
        assert again.counts_by_kind() == {}
        assert again.relations() == [_relation(), PendingRelation("r-2", "a-1", "b-9", "precedes", 3)]
        assert again.parents() == [_parent()]
        assert all(again.is_trusted(row) for row in [*again.relations(), *again.parents()])
        again.rollback()

    def test_put_replaces_by_relation_and_by_child(self, tmp_path):
        store = _store(tmp_path / "links.sqlite")
        store.put_relation(_relation(to_ref="b-1"))
        store.put_relation(_relation(to_ref="b-2"))
        store.put_parent(_parent(parent_ref="a-1"))
        store.put_parent(_parent(parent_ref="a-2"))
        assert [r.to_ref for r in store.relations()] == ["b-2"]
        assert [p.parent_ref for p in store.parents()] == ["a-2"]
        store.rollback()

    def test_a_file_with_only_links_waiting_is_kept_and_deleted_once_they_are_gone(self, tmp_path):
        path = tmp_path / "links.sqlite"
        store = _store(path)
        store.put_relation(_relation())
        store.put_parent(_parent())
        assert store.commit() is True

        store = _store(path)
        store.delete_relation("r-1")
        assert store.commit() is True
        store = _store(path)
        store.delete_parent("b-2")
        assert store.commit() is False
        assert not path.exists()

    @pytest.mark.parametrize(
        ("table", "column", "value"),
        [
            ("pending_relation", "to_ref", "b-7"),
            ("pending_relation", "relation_type", "duplicates"),
            ("pending_relation", "delay", 4),
            ("pending_parent", "parent_ref", "a-9"),
        ],
    )
    def test_a_tampered_row_is_not_trusted(self, tmp_path, table, column, value):
        path = tmp_path / "links.sqlite"
        store = LinksStore.open(path, SYSTEM, INSTANCE, writable=True, key=KEY_A)
        store.put_relation(_relation())
        store.put_parent(_parent())
        store.commit()
        conn = sqlite3.connect(path)
        conn.execute(f"UPDATE {table} SET {column} = ?", (value,))  # noqa: S608
        conn.commit()
        conn.close()
        reader = LinksStore.open(path, SYSTEM, INSTANCE, writable=False, key=KEY_A)
        rows = reader.relations() if table == "pending_relation" else reader.parents()
        assert [reader.is_trusted(row) for row in rows] == [False]
        reader.rollback()

    def test_a_row_signed_by_another_installation_is_not_trusted(self, tmp_path):
        path = tmp_path / "links.sqlite"
        store = LinksStore.open(path, SYSTEM, INSTANCE, writable=True, key=KEY_B)
        store.put_relation(_relation())
        store.put_parent(_parent())
        store.commit()
        reader = LinksStore.open(path, SYSTEM, INSTANCE, writable=False, key=KEY_A)
        assert [reader.is_trusted(row) for row in [*reader.relations(), *reader.parents()]] == [False, False]
        reader.rollback()

    def test_a_relations_signature_does_not_fit_a_parent(self, tmp_path):
        """Each kind of row is signed as that kind, so a signature cannot be moved to another table."""
        store = LinksStore.open(tmp_path / "links.sqlite", SYSTEM, INSTANCE, writable=True, key=KEY_A)
        relation = PendingRelation("b-2", "a-2", "x", "relates", None)
        parent = replace(_parent("b-2", "a-2"), signature=store.sign(relation))
        assert store.is_trusted(parent) is False
        store.rollback()

    @pytest.mark.parametrize(
        "row",
        [
            PendingRelation("r-1", "a-1", "b 1", "blocks", None),
            PendingRelation("r-1", "a-1", "b-1\n", "blocks", None),
            PendingRelation("r-1", "a-1", "", "blocks", None),
            PendingRelation("r-1", "a-1", "b-1", "owns", None),
            PendingRelation("r-1", "a-1", "b-1", "precedes", "3"),  # type: ignore[arg-type]
            PendingRelation("r-1", "a-1", "a-1", "relates", None),
            PendingParent("b-2", "x" * 256),
            PendingParent("b-2", "b-2"),
            PendingParent("b-{2}", "a-2"),
        ],
    )
    def test_a_malformed_row_is_not_trusted_even_when_signed(self, tmp_path, row):
        store = LinksStore.open(tmp_path / "links.sqlite", SYSTEM, INSTANCE, writable=True, key=KEY_A)
        assert store.is_trusted(replace(row, signature=store.sign(row))) is False
        store.rollback()
