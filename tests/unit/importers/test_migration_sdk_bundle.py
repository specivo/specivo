"""A bundle written with the migration SDK passes core's validation, and the two validators agree.

The SDK (``tools/migration-sdk``) is a separate project that reimplements the
format for importers that do not link to core. These tests hold it to core's
reading of the format: what the SDK writes core accepts, and on a bundle with a
mistake both report the same problems.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from specivo.importers.bundle import BundleSourceAdapter
from specivo.importers.bundle.report import ValidationReport
from tests.fixtures import migration_sdk as migration_sdk_fixture
from tests.fixtures.import_bundle import FULL, issue, manifest, project, stored, user, within_seconds, write_bundle
from tests.fixtures.migration_sdk import migration_sdk, write_sdk_bundle

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
        sdk = migration_sdk()
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
            {"users": [user("u1\n")]},
            {
                "projects": [project("p1")],
                "issues": [issue("i1")],
                "journals": [
                    {
                        "source_ref": "j1",
                        "issue_ref": "i1",
                        "details": [{"property": "attr", "prop_key": "description", "new_value": "{{wiki:nope}}"}],
                    }
                ],
            },
            {"projects": [project("p1")], "issues": [issue("i1", description="{{issue:a-7}} {{issue:a-8|older}}")]},
        ],
        ids=[
            "unresolved",
            "duplicate",
            "bad-date",
            "bad-json",
            "cycle",
            "placeholders",
            "group-assignee",
            "newline-ref",
            "description-change",
            "issue-outside-bundle",
        ],
    )
    async def test_on_a_bundle_with_a_mistake(self, tmp_path, records):
        root = write_bundle(tmp_path / "b", **records)
        sdk_report = migration_sdk().validate_bundle(root)
        core_report = await _core(root)
        assert _problems(sdk_report) == _problems(core_report)
        assert [p.message for p in sdk_report.problems] == [p.message for p in core_report.problems]

    async def test_on_a_bad_manifest_and_counts(self, tmp_path):
        root = write_bundle(tmp_path / "b", manifest_data=manifest(counts={"users": 2}), users=[user("u1")])
        assert _problems(migration_sdk().validate_bundle(root)) == _problems(await _core(root))

    async def test_on_a_tampered_file(self, tmp_path):
        root = write_sdk_bundle(tmp_path / "bundle")
        stored = next((root / "files").iterdir())
        stored.write_bytes(b"tampered")
        sdk_report = migration_sdk().validate_bundle(root)
        assert not sdk_report.ok
        assert _problems(sdk_report) == _problems(await _core(root))

    async def test_on_a_zip(self, tmp_path):
        root = write_sdk_bundle(tmp_path / "bundle")
        (root / "manifest.json").write_text(json.dumps({**json.loads((root / "manifest.json").read_text()), "x": 1}))
        archive = Path(shutil.make_archive(str(tmp_path / "bundle"), "zip", root))
        sdk_report = migration_sdk().validate_bundle(archive)
        assert not sdk_report.ok
        assert _problems(sdk_report) == _problems(await _core(archive))


class TestTheValidatorsAgreeOnASeparateFilesDirectory:
    """``files_path`` in the SDK and ``--files-path`` in core give the same verdicts."""

    def _case(self, tmp_path: Path, case: str) -> tuple[Path, Path]:
        attachment = {
            "source_ref": "a1",
            "container_kind": "issue",
            "container_ref": "i1",
            "filename": "log.txt",
            "sha256": stored(b"x"),
            "filesize": 1,
        }
        bundle = write_bundle(tmp_path / "b", projects=[project("p1")], issues=[issue("i1")], attachments=[attachment])
        files = tmp_path / "store"
        files.mkdir()
        if case == "valid":
            (files / stored(b"x")).write_bytes(b"x")
        elif case == "tampered":
            (files / stored(b"x")).write_bytes(b"y")
        elif case == "symlink":
            (tmp_path / "secret").write_bytes(b"x")
            os.symlink(tmp_path / "secret", files / stored(b"x"))
        elif case == "extra":
            (files / stored(b"x")).write_bytes(b"x")
            (files / stored(b"unused")).write_bytes(b"unused")
            (files / "README.txt").write_text("hello")
        elif case == "absent":
            files.rmdir()
        return bundle, files

    @pytest.mark.parametrize("case", ["valid", "missing", "tampered", "symlink", "extra", "absent"])
    async def test_on_each_case(self, tmp_path, case):
        sdk = migration_sdk()
        bundle, files = self._case(tmp_path, case)
        sdk_report = sdk.validate_bundle(bundle, files_path=files)
        core_report = await BundleSourceAdapter(bundle, files_path=files).validate()
        assert _problems(sdk_report) == _problems(core_report)
        assert [p.message for p in sdk_report.problems] == [p.message for p in core_report.problems]


SECRET_HASH = "$2y$10$Q9mWkzVtH3secretvaluexyzAbCdEfGhIjKlMnOpQrStUvWxYz012"


class TestTheValidatorsAgreeOnSecretsAndUnsafeEntries:
    """A bad credential is reported without its value, and non-regular files are refused, the same way by both."""

    @pytest.mark.parametrize(
        "credential",
        [
            {"algorithm": "", "fields": {"hash": SECRET_HASH}},
            {"algorithm": "bcrypt", "fields": {"hash": [SECRET_HASH]}},
            {"algorithm": "bcrypt", "fields": {"hash": "x"}, "salt": SECRET_HASH},
            SECRET_HASH,
        ],
        ids=["empty-algorithm", "hash-not-a-string", "unknown-property", "not-an-object"],
    )
    async def test_on_a_bad_credential(self, tmp_path, credential):
        root = write_bundle(tmp_path / "b", users=[user("u1", credential=credential)])
        sdk_report = migration_sdk().validate_bundle(root)
        core_report = await _core(root)
        assert _problems(sdk_report) == _problems(core_report)
        assert [p.message for p in sdk_report.problems] == [p.message for p in core_report.problems]
        for report in (sdk_report, core_report):
            assert SECRET_HASH not in report.format_text()

    @pytest.mark.parametrize(
        "case", ["fifo-record-file", "fifo-manifest", "directory-record-file", "fifo-in-files", "fifo-in-files-path"]
    )
    def test_on_a_non_regular_file(self, tmp_path, case):
        sdk = migration_sdk()
        # With a separate files directory the bundle has no files/ of its own to be warned about.
        own_files = None if case == "fifo-in-files-path" else {stored(b"x"): b"x"}
        root = write_bundle(tmp_path / "b", users=[user("u1")], files=own_files)
        files_path = None
        if case == "fifo-record-file":
            target = root / "issues.jsonl"
            os.mkfifo(target)
        elif case == "fifo-manifest":
            target = root / "manifest.json"
            target.unlink()
            os.mkfifo(target)
        elif case == "directory-record-file":
            target = root / "users.jsonl"
            target.unlink()
            target.mkdir()
        elif case == "fifo-in-files":
            target = root / "files" / stored(b"y")
            os.mkfifo(target)
        else:
            files_path = tmp_path / "store"
            files_path.mkdir()
            target = files_path / stored(b"y")
            os.mkfifo(target)

        sdk_report = within_seconds(5, lambda: sdk.validate_bundle(root, files_path=files_path), target)
        core_report = within_seconds(
            5, lambda: asyncio.run(BundleSourceAdapter(root, files_path=files_path).validate()), target
        )
        assert [str(p.code) for p in core_report.errors] == ["unsafe_entry"]
        assert _problems(sdk_report) == _problems(core_report)
        assert [p.message for p in sdk_report.problems] == [p.message for p in core_report.problems]


class TestTheSdkMustBeThere:
    def test_without_it_the_cross_check_skips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(migration_sdk_fixture, "SDK_SRC", tmp_path / "missing")
        monkeypatch.delenv("SPECIVO_REQUIRE_MIGRATION_SDK", raising=False)
        with pytest.raises(pytest.skip.Exception):
            migration_sdk_fixture.migration_sdk()

    def test_when_required_a_missing_sdk_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(migration_sdk_fixture, "SDK_SRC", tmp_path / "missing")
        monkeypatch.setenv("SPECIVO_REQUIRE_MIGRATION_SDK", "1")
        with pytest.raises(pytest.fail.Exception, match="SPECIVO_REQUIRE_MIGRATION_SDK"):
            migration_sdk_fixture.migration_sdk()
