"""Unit tests for the import command's argument handling."""

from __future__ import annotations

import argparse

import pytest

from specivo.cli.import_redmine import _parse_list, _parse_map, build_parser

pytestmark = pytest.mark.unit


class TestMapOption:
    def test_single_pair(self):
        assert _parse_map("acme-app=ACME") == {"acme-app": "ACME"}

    def test_several_pairs(self):
        assert _parse_map("a=A,b=B") == {"a": "A", "b": "B"}

    def test_spaces_are_tolerated(self):
        """People type these by hand."""
        assert _parse_map(" a = A , b = B ") == {"a": "A", "b": "B"}

    def test_value_may_contain_spaces(self):
        assert _parse_map("In Progress=active") == {"In Progress": "active"}

    def test_empty_is_no_mapping(self):
        assert _parse_map("") == {}
        assert _parse_map(None) == {}

    def test_trailing_comma_is_ignored(self):
        assert _parse_map("a=A,") == {"a": "A"}

    def test_missing_equals_is_rejected(self):
        """Silently ignoring it would drop an override the operator meant."""
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_map("acme-app")


class TestListOption:
    def test_single_value(self):
        assert _parse_list("1") == ("1",)

    def test_several_values(self):
        assert _parse_list("1,2,3") == ("1", "2", "3")

    def test_spaces_are_tolerated(self):
        assert _parse_list(" 1 , 2 ") == ("1", "2")

    def test_empty_means_everything(self):
        assert _parse_list("") is None
        assert _parse_list(None) is None
        assert _parse_list(",,") is None


class TestParser:
    def _parse(self, *args):
        return build_parser().parse_args(["--source-db-url", "postgresql://u:p@h/redmine", *args])

    def test_source_url_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_defaults_are_conservative(self):
        """An import writes by default only when asked; nothing else is assumed."""
        args = self._parse()
        assert args.dry_run is False
        assert args.strict is False
        assert args.merge_duplicate_statuses is False
        assert args.flatten_excess_depth is False
        assert args.project is None
        assert args.batch_size == 500

    def test_flags_are_parsed(self):
        args = self._parse("--dry-run", "--strict", "--merge-duplicate-statuses", "--flatten-excess-depth")
        assert args.dry_run is True
        assert args.strict is True
        assert args.merge_duplicate_statuses is True
        assert args.flatten_excess_depth is True

    def test_resume_takes_a_run_id(self):
        args = self._parse("--resume", "0f6b5d64-6b6e-4a58-9d2c-7f1f5c5d0f1a")
        assert args.resume == "0f6b5d64-6b6e-4a58-9d2c-7f1f5c5d0f1a"

    def test_files_dir_and_instance(self):
        args = self._parse("--source-files-dir", "/var/redmine/files", "--source-instance", "tracker")
        assert args.source_files_dir == "/var/redmine/files"
        assert args.source_instance == "tracker"

    def test_help_points_at_the_dry_run(self):
        """It is the first thing anyone should run."""
        assert "--dry-run" in build_parser().format_help()
