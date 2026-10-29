"""Unit tests for ``admin import-map``: option handling and the scope listing.

The database side (``list_scopes``, ``rename_scope``) is covered by
``tests/services/test_import_id_map.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from specivo.cli.admin import build_parser, format_pending, format_purge, format_resolve, format_scopes, rename_targets
from specivo.importers.core.id_map import IdMapScope, ScopeRenameError
from specivo.importers.load.pending_links import LinksOutcome

pytestmark = pytest.mark.unit


def _rename(*argv: str):
    return build_parser().parse_args(["import-map", "rename", *argv])


class TestRenameOptions:
    def test_one_system_and_a_new_instance(self):
        args = _rename("--system", "redmine", "--from", "postgresql://redmine:***@db/redmine", "--to", "tracker")
        assert rename_targets(args) == ("redmine", "postgresql://redmine:***@db/redmine", "redmine", "tracker")

    def test_the_long_spellings(self):
        args = _rename(
            "--from-system", "bundle", "--to-system", "redmine", "--from-instance", "redmine:t", "--to-instance", "t"
        )
        assert rename_targets(args) == ("bundle", "redmine:t", "redmine", "t")

    def test_only_the_system_changes(self):
        args = _rename("--from-system", "bundle", "--to-system", "redmine", "--from", "t")
        assert rename_targets(args) == ("bundle", "t", "redmine", "t")

    def test_without_yes_it_is_a_dry_run(self):
        args = _rename("--system", "redmine", "--from", "a", "--to", "b")
        assert (args.yes, args.dry_run) == (False, False)

    @pytest.mark.parametrize("argv", [("--from", "a", "--to", "b"), ("--system", "redmine", "--to", "b")])
    def test_the_old_scope_must_be_named(self, argv):
        with pytest.raises(ScopeRenameError):
            rename_targets(_rename(*argv))

    def test_list_takes_no_options(self):
        assert build_parser().parse_args(["import-map", "list"]).import_map_command == "list"


class TestListing:
    def test_an_empty_map(self):
        assert "nothing has been imported" in format_scopes([])

    def test_one_block_per_scope(self):
        scope = IdMapScope(
            "redmine",
            "tracker.example.com",
            {"user": 3, "issue": 12},
            runs=2,
            last_imported_at=datetime(2026, 1, 5, 9, 30, tzinfo=UTC),
        )
        assert format_scopes([scope]).splitlines() == [
            "redmine @ tracker.example.com",
            "  15 row(s) from 2 run(s), last written 2026-01-05T09:30:00+00:00",
            "  issue  12",
            "  user   3",
        ]


class TestLinksCommands:
    def test_pending_needs_the_links_file(self, capsys):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["import-map", "pending"])
        args = build_parser().parse_args(["import-map", "pending", "--links-file", "/srv/import/links.sqlite"])
        assert (args.import_map_command, args.links_file) == ("pending", "/srv/import/links.sqlite")

    def test_resolve_pending_is_a_dry_run_without_yes(self):
        args = build_parser().parse_args(["import-map", "resolve-pending", "--links-file", "l.sqlite"])
        assert (args.import_map_command, args.yes) == ("resolve-pending", False)
        assert build_parser().parse_args(["import-map", "resolve-pending", "--links-file", "l", "--yes"]).yes

    def test_nothing_pending(self):
        assert format_pending(Path("l.sqlite"), None, 0, {}) == (
            "Nothing is waiting in l.sqlite: no imported text, relation or parent names a record that has not been "
            "imported."
        )

    def test_the_pending_listing(self):
        listing = format_pending(Path("l.sqlite"), ("sample", "tracker.example.com"), 3, {"issue": 2, "wiki": 1})
        assert listing.splitlines() == [
            "sample @ tracker.example.com: 3 text(s) wait for records not imported yet",
            "  issue  2",
            "  wiki   1",
        ]

    def test_the_pending_listing_with_relations_and_parents(self):
        scope = ("sample", "tracker.example.com")
        assert format_pending(Path("l.sqlite"), scope, 0, {}, {"relation": 2, "parent": 1}).splitlines() == [
            "sample @ tracker.example.com: 2 relation(s) and 1 issue parent(s) wait for issues not imported yet",
        ]
        assert format_pending(Path("l.sqlite"), scope, 1, {"issue": 1}, {"parent": 1}).splitlines() == [
            "sample @ tracker.example.com: 1 text(s) wait for records not imported yet",
            "  issue  1",
            "sample @ tracker.example.com: 1 issue parent(s) wait for issues not imported yet",
        ]

    def test_resolve_pending_reports_relations_and_parents(self):
        outcome = LinksOutcome(
            Path("l.sqlite"),
            ("sample", "t"),
            relations_linked=2,
            parents_linked=1,
            links_waiting=3,
            link_problems=["relation r-d: Cannot create a relation between a parent issue and its descendant."],
        )
        assert format_resolve(outcome, dry_run=False).splitlines() == [
            "Linked 0 text(s); 0 left alone because they were edited after import; 0 still waiting. "
            "Made 2 relation(s) and set 1 parent(s); 1 refused or left alone; 3 still waiting.",
            "  relation r-d: Cannot create a relation between a parent issue and its descendant.",
        ]
        assert format_resolve(outcome, dry_run=True).startswith(
            "0 text(s) would be linked, 0 left alone because they were edited after import, 0 still waiting. "
            "2 relation(s) would be made and 1 parent(s) set; 1 refused or left alone; 3 still waiting. "
        )

    def test_a_dry_run_of_resolve_pending(self):
        outcome = LinksOutcome(
            Path("l.sqlite"), ("sample", "t"), linked=[("issue", 4)], edited=[("journal", 9)], gone=0, waiting=2
        )
        assert format_resolve(outcome, dry_run=True) == (
            "1 text(s) would be linked, 1 left alone because they were edited after import (journal 9), "
            "2 still waiting. Nothing was changed; re-run with --yes to write them."
        )

    def test_resolve_pending_with_yes(self):
        outcome = LinksOutcome(Path("l.sqlite"), ("sample", "t"), linked=[("issue", 4)], edited=[], gone=0, waiting=0)
        assert format_resolve(outcome, dry_run=False) == (
            "Linked 1 text(s); 0 left alone because they were edited after import; 0 still waiting."
        )

    def test_resolve_pending_with_nothing_waiting(self):
        outcome = LinksOutcome(Path("l.sqlite"), None, linked=[], edited=[], gone=0, waiting=0)
        assert format_resolve(outcome, dry_run=False) == "Nothing is waiting in l.sqlite."


class TestPurge:
    def test_purge_needs_the_scope(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["import-map", "purge", "--system", "redmine"])
        args = build_parser().parse_args(["import-map", "purge", "--system", "redmine", "--instance", "t"])
        assert (args.system, args.instance, args.yes) == ("redmine", "t", False)

    def test_the_help_warns_that_delta_imports_end(self):
        parser = build_parser()
        purge = (
            parser._subparsers._group_actions[0].choices["import-map"]._subparsers._group_actions[0].choices["purge"]
        )
        assert "delta imports" in purge.format_help().replace("\n", " ")

    def test_the_messages(self):
        assert format_purge("redmine", "t", 0, dry_run=True) == "The scope redmine @ t has no rows; nothing to purge."
        assert format_purge("redmine", "t", 5, dry_run=True) == (
            "5 row(s) of redmine @ t would be deleted. Nothing was changed; re-run with --yes to delete them."
        )
        assert format_purge("redmine", "t", 5, dry_run=False).startswith("Deleted 5 row(s) of redmine @ t.")

    def test_resolve_pending_names_the_rows_it_ignored(self):
        outcome = LinksOutcome(Path("l.sqlite"), ("sample", "t"), untrusted=2, foreign=1)
        assert format_resolve(outcome, dry_run=False).endswith(
            "Ignored and removed: 2 row(s) this installation did not write, "
            "1 naming records this source did not import."
        )
