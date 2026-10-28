"""Unit tests for the generic import command's argument handling."""

from __future__ import annotations

import argparse
import json
import uuid

import pytest

from specivo.cli.import_data import build_options, format_source_list, main, parse_args, run_validation
from specivo.importers.bundle import BundleSourceAdapter
from specivo.importers.core.cli_args import parse_list, parse_map
from specivo.importers.core.pipeline import ImportPhase
from specivo.importers.core.source import BaseSourceAdapter, build_source_registry
from tests.fixtures.import_bundle import FULL, user, write_bundle

pytestmark = pytest.mark.unit

OTHER_SOURCE_OPTION = "postgresql://u:p@h/other"


@pytest.fixture
def registry():
    """The registered sources: ``bundle`` only."""
    return build_source_registry()


class TestMapOption:
    def test_single_pair(self):
        assert parse_map("acme-app=ACME") == {"acme-app": "ACME"}

    def test_several_pairs(self):
        assert parse_map("a=A,b=B") == {"a": "A", "b": "B"}

    def test_spaces_are_tolerated(self):
        """People type these by hand."""
        assert parse_map(" a = A , b = B ") == {"a": "A", "b": "B"}

    def test_value_may_contain_spaces(self):
        assert parse_map("In Progress=active") == {"In Progress": "active"}

    def test_empty_is_no_mapping(self):
        assert parse_map("") == {}
        assert parse_map(None) == {}

    def test_trailing_comma_is_ignored(self):
        assert parse_map("a=A,") == {"a": "A"}

    def test_missing_equals_is_rejected(self):
        """Silently ignoring it would drop an override the operator meant."""
        with pytest.raises(argparse.ArgumentTypeError):
            parse_map("acme-app")


class TestListOption:
    def test_single_value(self):
        assert parse_list("1") == ("1",)

    def test_several_values(self):
        assert parse_list("1,2,3") == ("1", "2", "3")

    def test_spaces_are_tolerated(self):
        assert parse_list(" 1 , 2 ") == ("1", "2")

    def test_empty_means_everything(self):
        assert parse_list("") is None
        assert parse_list(None) is None
        assert parse_list(",,") is None


class TestSourceSelection:
    def test_list_sources_names_the_bundle_source(self, registry, capsys):
        with pytest.raises(SystemExit) as exc:
            parse_args(["--list-sources"], registry)
        assert exc.value.code == 0
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 1
        assert lines[0].split()[0] == "bundle"
        assert "import bundle" in lines[0]

    def test_an_unknown_source_fails_with_the_known_names(self, registry, capsys):
        with pytest.raises(SystemExit) as exc:
            parse_args(["--source", "nope"], registry)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "Unknown import source 'nope'. Registered sources: bundle" in err
        assert "redmine-migration-tool" not in err

    def test_a_missing_source_fails_with_the_known_names(self, registry, capsys):
        with pytest.raises(SystemExit) as exc:
            parse_args(["--dry-run"], registry)
        assert exc.value.code == 2
        assert "--source is required. Registered sources: bundle" in capsys.readouterr().err

    def test_help_without_a_source_lists_the_sources(self, registry, capsys):
        with pytest.raises(SystemExit) as exc:
            parse_args(["--help"], registry)
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Registered sources: bundle" in out
        assert "--dry-run" in out
        assert "--source-db-url" not in out

    def test_a_sources_help_shows_its_own_options(self, registry, capsys):
        with pytest.raises(SystemExit):
            parse_args(["--source", "bundle", "--help"], registry)
        out = capsys.readouterr().out
        assert "bundle source:" in out
        assert "--path" in out
        assert "--import-passwords" in out
        assert "--source-db-url" not in out

    def test_a_sources_required_option_is_enforced(self, registry):
        with pytest.raises(SystemExit):
            parse_args(["--source", "bundle"], registry)

    def test_another_sources_option_is_rejected(self, registry):
        with pytest.raises(SystemExit):
            parse_args(["--source", "bundle", "--path", str(FULL), "--source-db-url", OTHER_SOURCE_OPTION], registry)


class TestRunOptions:
    def _parse(self, registry, *args):
        return parse_args(["--source", "bundle", "--path", str(FULL), *args], registry)

    def test_the_source_class_is_returned(self, registry):
        assert self._parse(registry)[1] is BundleSourceAdapter

    def test_defaults_are_conservative(self, registry):
        """An import writes by default only when asked; nothing else is assumed."""
        args, _ = self._parse(registry)
        assert args.dry_run is False
        assert args.strict is False
        assert args.merge_duplicate_statuses is False
        assert args.flatten_excess_depth is False
        assert args.project is None
        assert args.batch_size == 500
        assert args.import_passwords is False
        assert args.source_instance is None

    def test_passwords_are_imported_only_when_asked(self, registry):
        assert self._parse(registry, "--import-passwords")[0].import_passwords is True

    def test_flags_are_parsed(self, registry):
        args, _ = self._parse(registry, "--dry-run", "--strict", "--merge-duplicate-statuses", "--flatten-excess-depth")
        assert args.dry_run is True
        assert args.strict is True
        assert args.merge_duplicate_statuses is True
        assert args.flatten_excess_depth is True

    def test_resume_takes_a_run_id(self, registry):
        args, _ = self._parse(registry, "--resume", "0f6b5d64-6b6e-4a58-9d2c-7f1f5c5d0f1a")
        assert args.resume == uuid.UUID("0f6b5d64-6b6e-4a58-9d2c-7f1f5c5d0f1a")

    def test_resume_rejects_anything_but_a_run_id(self, registry):
        with pytest.raises(SystemExit):
            self._parse(registry, "--resume", "yesterday")

    def test_stop_after_phase_takes_a_phase(self, registry):
        assert self._parse(registry, "--stop-after-phase", "users")[0].stop_after_phase is ImportPhase.USERS

    def test_an_unknown_phase_lists_the_phases(self, registry, capsys):
        with pytest.raises(SystemExit):
            self._parse(registry, "--stop-after-phase", "nope")
        assert "bootstrap" in capsys.readouterr().err

    def test_scope_and_key_map_are_parsed(self, registry):
        args, _ = self._parse(registry, "--project", "1, 2", "--project-key-map", "acme-app=ACME")
        assert args.project == ("1", "2")
        assert args.project_key_map == {"acme-app": "ACME"}

    def test_options_carry_the_run_settings(self, registry):
        args, cls = self._parse(registry, "--dry-run", "--project", "3", "--import-passwords", "--batch-size", "50")
        options = build_options(args, cls.from_args(args))
        assert options.dry_run is True
        assert options.project_refs == ("3",)
        assert options.import_passwords is True
        assert options.batch_size == 50
        assert options.project_key_map == {}

    def test_help_points_at_the_dry_run(self, registry, capsys):
        """It is the first thing anyone should run."""
        with pytest.raises(SystemExit):
            parse_args(["--help"], registry)
        assert "--dry-run" in capsys.readouterr().out


class TestBundleSource:
    def _parse(self, registry, *args):
        return parse_args(["--source", "bundle", "--path", str(FULL), *args], registry)

    def test_the_bundle_source_reads_a_path(self, registry):
        args, cls = self._parse(registry)
        assert cls is BundleSourceAdapter
        adapter = cls.from_args(args)
        assert adapter.path == FULL
        assert build_options(args, adapter).source_instance == "tracker.example.com"

    def test_attachments_can_come_from_a_directory_of_their_own(self, registry, tmp_path):
        args, cls = self._parse(registry, "--files-path", str(tmp_path))
        assert cls.from_args(args).files_path == tmp_path

    def test_adding_to_an_earlier_import_is_an_explicit_option(self, registry):
        args, cls = self._parse(registry)
        assert build_options(args, cls.from_args(args)).continue_import is False
        args, cls = self._parse(registry, "--continue-import")
        assert build_options(args, cls.from_args(args)).continue_import is True

    def test_by_default_they_come_from_the_bundle(self, registry):
        args, cls = self._parse(registry)
        assert cls.from_args(args).files_path is None

    def test_the_path_is_required(self, registry):
        with pytest.raises(SystemExit):
            parse_args(["--source", "bundle"], registry)

    def test_validate_and_dry_run_are_two_different_things(self, registry, capsys):
        with pytest.raises(SystemExit) as exc:
            self._parse(registry, "--validate", "--dry-run")
        assert exc.value.code == 2
        assert "not allowed with argument" in capsys.readouterr().err

    async def test_a_valid_bundle_validates_with_exit_code_0(self, registry, capsys, tmp_path):
        report = tmp_path / "report.json"
        args, cls = self._parse(registry, "--validate", "--report-json", str(report))
        assert await run_validation(args, cls.from_args(args)) == 0
        assert "valid — 0 error(s), 0 warning(s)" in capsys.readouterr().out
        assert json.loads(report.read_text())["valid"] is True

    async def test_an_invalid_bundle_exits_1_and_says_where(self, registry, capsys, tmp_path):
        root = write_bundle(tmp_path / "b", users=[user("u1"), user("u1")])
        args, cls = parse_args(["--source", "bundle", "--path", str(root), "--validate"], registry)
        assert await run_validation(args, cls.from_args(args)) == 1
        assert "users.jsonl:2: error: Duplicate source_ref 'u1'" in capsys.readouterr().out

    async def test_a_source_that_cannot_validate_itself_says_so(self, registry):
        class Plain(BaseSourceAdapter):
            source_system = "plain"

        args, _ = self._parse(registry, "--validate")
        assert await run_validation(args, Plain()) == 2


class TestFormatting:
    def test_source_list_is_one_line_per_source(self, registry):
        assert format_source_list(registry).splitlines() == [
            "bundle  An import bundle (directory or .zip) written by an importer"
        ]


class TestMain:
    def test_main_bootstraps_plugins_before_parsing(self, monkeypatch, capsys):
        """A bundle's credentials need their plugin's hasher registered in this process too."""
        import specivo.services.password_hashers as hashers_mod
        from specivo.core.config import get_settings

        monkeypatch.setattr(get_settings(), "installed_plugins", ["tests.fixtures.acme_hashers.Plugin"])
        monkeypatch.setattr(hashers_mod, "_registry", None)

        with pytest.raises(SystemExit) as exc:
            main(["--list-sources"])

        assert exc.value.code == 0
        assert capsys.readouterr().out.startswith("bundle")
        assert hashers_mod.get_password_hasher_registry().get("acme_md5") is not None

    def test_a_misconfigured_plugin_stops_the_command(self, monkeypatch):
        import specivo.services.password_hashers as hashers_mod
        from specivo.core.config import get_settings

        monkeypatch.setattr(get_settings(), "installed_plugins", ["tests.fixtures.no_such_plugin.Plugin"])
        monkeypatch.setattr(hashers_mod, "_registry", None)

        with pytest.raises(SystemExit) as exc:
            main(["--list-sources"])
        assert exc.value.code == 1


def test_unknown_redmine_source_points_to_the_migration_tool(registry, capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(["--source", "redmine", "--source-db-url", OTHER_SOURCE_OPTION], registry)
    assert exc.value.code == 2
    err = capsys.readouterr().err.strip().splitlines()
    message = [line for line in err if "redmine" in line.lower() and "error" in line.lower()]
    assert len(message) == 1
    assert "https://github.com/specivo/redmine-migration-tool" in message[0]
    assert "--source bundle" in message[0]
