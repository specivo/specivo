"""Unit tests for ``admin import-map``: option handling and the scope listing.

The database side (``list_scopes``, ``rename_scope``) is covered by
``tests/services/test_import_id_map.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from specivo.cli.admin import build_parser, format_scopes, rename_targets
from specivo.importers.core.id_map import IdMapScope, ScopeRenameError

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
