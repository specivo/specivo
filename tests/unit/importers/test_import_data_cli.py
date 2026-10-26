"""Unit tests for the generic import command's argument handling."""

from __future__ import annotations

import argparse
import uuid

import pytest

from specivo.cli.import_data import build_options, format_source_list, main, parse_args
from specivo.importers.core.cli_args import parse_list, parse_map
from specivo.importers.core.pipeline import ImportPhase
from specivo.importers.core.source import BaseSourceAdapter, build_source_registry
from specivo.importers.redmine import RedmineSourceAdapter
from tests.fixtures.acme_import import AcmeJsonSource

pytestmark = pytest.mark.unit

REDMINE_URL = "postgresql://u:p@h/redmine"


@pytest.fixture
def registry():
    """The built-in sources plus the guide's example plugin."""
    return build_source_registry([("acme-import", [AcmeJsonSource])])


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
    def test_list_sources_names_each_source_and_its_origin(self, registry, capsys):
        with pytest.raises(SystemExit) as exc:
            parse_args(["--list-sources"], registry)
        assert exc.value.code == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0].split()[:2] == ["acme", "plugin:acme-import"]
        assert lines[1].split()[:2] == ["redmine", "core"]
        assert "Acme Tracker JSON export" in lines[0]

    def test_an_unknown_source_fails_with_the_known_names(self, registry, capsys):
        with pytest.raises(SystemExit) as exc:
            parse_args(["--source", "nope"], registry)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "Unknown import source 'nope'" in err
        assert "acme, redmine" in err

    def test_a_missing_source_fails_with_the_known_names(self, registry, capsys):
        with pytest.raises(SystemExit) as exc:
            parse_args(["--dry-run"], registry)
        assert exc.value.code == 2
        assert "--source is required. Registered sources: acme, redmine" in capsys.readouterr().err

    def test_help_without_a_source_lists_the_sources(self, registry, capsys):
        with pytest.raises(SystemExit) as exc:
            parse_args(["--help"], registry)
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Registered sources: acme, redmine" in out
        assert "--dry-run" in out
        assert "--source-db-url" not in out

    def test_a_sources_help_shows_its_own_options(self, registry, capsys):
        with pytest.raises(SystemExit):
            parse_args(["--source", "acme", "--help"], registry)
        out = capsys.readouterr().out
        assert "acme source:" in out
        assert "--export" in out
        assert "--import-passwords" in out
        assert "--source-db-url" not in out

    def test_redmines_help_shows_the_redmine_options(self, registry, capsys):
        with pytest.raises(SystemExit):
            parse_args(["--source", "redmine", "--help"], registry)
        out = capsys.readouterr().out
        for option in ("--source-db-url", "--source-files-dir", "--status-category-map", "--cf-key-map"):
            assert option in out
        assert "--export" not in out

    def test_a_sources_required_option_is_enforced(self, registry):
        with pytest.raises(SystemExit):
            parse_args(["--source", "redmine"], registry)

    def test_another_sources_option_is_rejected(self, registry):
        with pytest.raises(SystemExit):
            parse_args(["--source", "acme", "--export", "x.json", "--source-db-url", REDMINE_URL], registry)


class TestRunOptions:
    def _parse(self, registry, *args):
        return parse_args(["--source", "redmine", "--source-db-url", REDMINE_URL, *args], registry)

    def test_the_source_class_is_returned(self, registry):
        assert self._parse(registry)[1] is RedmineSourceAdapter

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


class TestRedmineFromArgs:
    def _adapter(self, registry, *args) -> RedmineSourceAdapter:
        parsed, cls = parse_args(["--source", "redmine", "--source-db-url", REDMINE_URL, *args], registry)
        return cls.from_args(parsed)

    def test_the_instance_defaults_to_the_database_url(self, registry):
        assert self._adapter(registry).source_instance == "postgresql://u:***@h/redmine"

    def test_files_dir_instance_and_maps(self, registry):
        adapter = self._adapter(
            registry,
            "--source-files-dir",
            "/var/redmine/files",
            "--source-instance",
            "tracker",
            "--status-category-map",
            "Awaiting Review=active",
            "--cf-key-map",
            "Story Points=points",
            "--batch-size",
            "25",
        )
        assert str(adapter.source_files_dir) == "/var/redmine/files"
        assert adapter.source_instance == "tracker"
        assert adapter._status_overrides == {"awaiting review": "active"}
        assert adapter._cf_key_overrides == {"story points": "points"}
        assert adapter._batch_size == 25


class TestPluginSourceFromArgs:
    def test_the_plugin_reads_its_own_option(self, registry):
        args, cls = parse_args(["--source", "acme", "--export", "/data/export.json"], registry)
        adapter = cls.from_args(args)
        assert isinstance(adapter, AcmeJsonSource)
        assert str(adapter.export_path) == "/data/export.json"
        assert adapter.source_instance == "acme"

    def test_source_instance_is_passed_through(self, registry):
        args, cls = parse_args(["--source", "acme", "--export", "x.json", "--source-instance", "eu"], registry)
        assert cls.from_args(args).source_instance == "eu"

    def test_a_base_adapter_needs_no_options(self):
        class Bare(BaseSourceAdapter):
            source_system = "bare"

        registry = build_source_registry([("bare-plugin", [Bare])])
        args, cls = parse_args(["--source", "bare"], registry)
        assert cls.from_args(args).source_instance == "bare"


class TestFormatting:
    def test_source_list_aligns_columns(self, registry):
        lines = format_source_list(registry).splitlines()
        assert lines[0].index("plugin:") == lines[1].index("core")


class TestMain:
    def test_main_bootstraps_plugins_before_parsing(self, monkeypatch, capsys):
        """A plugin's source and its hasher must reach the import, not only create_app()."""
        import specivo.importers.core.source as source_mod
        import specivo.services.password_hashers as hashers_mod
        from specivo.core.config import get_settings

        monkeypatch.setattr(
            get_settings(),
            "installed_plugins",
            ["tests.fixtures.acme_hashers.Plugin", "tests.fixtures.acme_import.Plugin"],
        )
        monkeypatch.setattr(hashers_mod, "_registry", None)
        monkeypatch.setattr(source_mod, "_registry", None)

        with pytest.raises(SystemExit) as exc:
            main(["--list-sources"])

        assert exc.value.code == 0
        assert "plugin:acme-import" in capsys.readouterr().out
        assert hashers_mod.get_password_hasher_registry().get("acme_md5") is not None

    def test_a_misconfigured_plugin_stops_the_command(self, monkeypatch):
        import specivo.importers.core.source as source_mod
        import specivo.services.password_hashers as hashers_mod
        from specivo.core.config import get_settings

        monkeypatch.setattr(get_settings(), "installed_plugins", ["tests.unit.importers.test_import_sources.Squatter"])
        monkeypatch.setattr(hashers_mod, "_registry", None)
        monkeypatch.setattr(source_mod, "_registry", None)

        with pytest.raises(SystemExit) as exc:
            main(["--list-sources"])
        assert exc.value.code == 1


class TestGuideExample:
    """The guide's plugin, export and listing are the tested fixtures and output."""

    GUIDE = "docs/guide/install/import-source-plugins.md"

    def _guide(self) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[3] / self.GUIDE).read_text()

    def _blocks(self, language: str) -> list[str]:
        import re

        return [block.strip() for block in re.findall(rf"```{language}\n(.*?)```", self._guide(), flags=re.S)]

    def test_the_guide_shows_the_tested_module_verbatim(self):
        import ast
        import inspect

        import tests.fixtures.acme_import as example

        source = inspect.getsource(example)
        docstring = ast.get_docstring(ast.parse(source), clean=False)
        body = source.split(f'"""{docstring}"""', 1)[1].strip()
        assert body in self._blocks("python")

    def test_the_guide_shows_the_tested_export(self):
        import json
        from pathlib import Path

        export = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "acme_export.json").read_text())
        assert export in [json.loads(block) for block in self._blocks("json")]

    def test_the_guide_shows_the_real_source_listing(self, registry):
        assert f"```\n{format_source_list(registry)}\n```" in self._guide()

    def test_the_guide_command_parses(self, registry):
        args, cls = parse_args(
            ["--source", "acme", "--export", "/data/acme-export.json", "--import-passwords", "--dry-run"], registry
        )
        assert cls is AcmeJsonSource
        assert "--export /data/acme-export.json --import-passwords --dry-run" in self._guide()
