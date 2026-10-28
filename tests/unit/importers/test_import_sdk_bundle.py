"""A bundle written with the import SDK passes core's validation, and the two validators agree.

The SDK (``tools/import-sdk``) is a separate project that reimplements the
format for importers that do not link to core. These tests hold it to core's
reading of the format: what the SDK writes core accepts, and on a bundle with a
mistake both report the same problems.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from specivo.importers.bundle import BundleSourceAdapter
from specivo.importers.bundle.report import ValidationReport
from tests.fixtures.import_bundle import FULL, issue, manifest, project, user, write_bundle
from tests.fixtures.import_sdk import import_sdk, write_sdk_bundle

pytestmark = pytest.mark.unit


async def _core(path: Path) -> ValidationReport:
    return await BundleSourceAdapter(path).validate()


def _problems(report) -> list[tuple[str, str, int | None, str | None, str]]:
    return [(str(p.severity), p.file, p.line, p.pointer, str(p.code)) for p in report.problems]


class TestWhatTheSdkWrites:
    @pytest.mark.parametrize("name", ["bundle", "bundle.zip"])
    async def test_core_accepts_it(self, tmp_path, name):
        report = await _core(write_sdk_bundle(tmp_path / name))
        assert report.ok, report.format_text()
        assert report.problems == []
        assert report.records["issues"] == 3
        assert report.records["wiki_versions"] == 3

    async def test_core_reads_back_every_record(self, tmp_path):
        adapter = BundleSourceAdapter(write_sdk_bundle(tmp_path / "bundle"))
        await adapter.connect()
        try:
            lookups = await adapter.extract_lookups()
            assert [s.source_ref for s in lookups.statuses] == ["open", "doing", "done"]
            issues = [i async for i in adapter.extract_issues("p1")]
            # Parents first, whatever order the writer was given.
            assert [i.source_ref for i in issues] == ["i1", "i2"]
            assert str(issues[0].estimated_hours) == "2.5"
            pages = [p async for p in adapter.extract_wiki_pages("p1")]
            assert [len(p.versions) for p in pages] == [2, 1]
        finally:
            await adapter.close()


class TestTheValidatorsAgree:
    def test_on_the_full_example(self):
        sdk = import_sdk()
        report = sdk.validate_bundle(FULL)
        assert report.ok, report.format_text()
        assert report.problems == []

    @pytest.mark.parametrize(
        "records",
        [
            {"projects": [project("p1")], "issues": [issue("i1", author_ref="ghost")]},
            {"users": [user("u1"), user("u1")]},
            {"users": [user("u1", created_at="2024-02-30T10:00:00Z")]},
            {"users": "{oops\n"},
            {"projects": [project("a", parent_ref="b"), project("b", parent_ref="a")]},
            {"projects": [project("p1")], "issues": [issue("i1", description="{{user:u1|Alex}} {{wiki:nope}}")]},
            {
                "groups": [{"source_ref": "g1", "name": "Team"}],
                "projects": [project("p1")],
                "issues": [issue("i1", assigned_to_ref="g1")],
            },
        ],
        ids=["unresolved", "duplicate", "bad-date", "bad-json", "cycle", "placeholders", "group-assignee"],
    )
    async def test_on_a_bundle_with_a_mistake(self, tmp_path, records):
        root = write_bundle(tmp_path / "b", **records)
        sdk_report = import_sdk().validate_bundle(root)
        core_report = await _core(root)
        assert _problems(sdk_report) == _problems(core_report)
        assert [p.message for p in sdk_report.problems] == [p.message for p in core_report.problems]

    async def test_on_a_bad_manifest_and_counts(self, tmp_path):
        root = write_bundle(tmp_path / "b", manifest_data=manifest(counts={"users": 2}), users=[user("u1")])
        assert _problems(import_sdk().validate_bundle(root)) == _problems(await _core(root))

    async def test_on_a_tampered_file(self, tmp_path):
        root = write_sdk_bundle(tmp_path / "bundle")
        stored = next((root / "files").iterdir())
        stored.write_bytes(b"tampered")
        sdk_report = import_sdk().validate_bundle(root)
        assert not sdk_report.ok
        assert _problems(sdk_report) == _problems(await _core(root))

    async def test_on_a_zip(self, tmp_path):
        root = write_sdk_bundle(tmp_path / "bundle")
        (root / "manifest.json").write_text(json.dumps({**json.loads((root / "manifest.json").read_text()), "x": 1}))
        archive = Path(shutil.make_archive(str(tmp_path / "bundle"), "zip", root))
        sdk_report = import_sdk().validate_bundle(archive)
        assert not sdk_report.ok
        assert _problems(sdk_report) == _problems(await _core(archive))
