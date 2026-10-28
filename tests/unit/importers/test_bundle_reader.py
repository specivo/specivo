"""Unit tests for reading and validating import bundles.

Each test writes a small bundle by hand, so a failure points at one rule. No
database: the reader only reads files.
"""

from __future__ import annotations

import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from specivo.importers.bundle import BundleInvalidError, BundleSourceAdapter
from specivo.importers.bundle.report import Severity, ValidationReport
from specivo.importers.core.ir import IRAttachment, IRIssue, IRProject, IRUser
from specivo.importers.core.placeholders import REFERENCE_STYLE_PLACEHOLDERS
from tests.fixtures.import_bundle import FULL, copy_full, issue, manifest, project, stored, user, write_bundle

pytestmark = pytest.mark.unit

SHA_LOG = "bb184ab3bffbeb05ed23843ec1ffebc22065b8f6ef3d9c11e4fd5cc0f043be44"


async def _validate(path: Path) -> ValidationReport:
    return await BundleSourceAdapter(path).validate()


def _codes(report: ValidationReport, severity: Severity = Severity.ERROR) -> list[tuple[str, int | None, str]]:
    return [(p.file, p.line, str(p.code)) for p in report.problems if p.severity is severity]


def _zip(path: Path, entries: dict[str, bytes], *, links: dict[str, str] | None = None) -> Path:
    """Write a zip with *entries*, and *links* as symbolic-link entries."""
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        for name, target in (links or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, target)
    return path


def _bundle_entries(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


class TestAValidBundle:
    async def test_the_full_example_is_valid(self):
        report = await _validate(FULL)
        assert report.ok, report.format_text()
        assert report.problems == []
        assert report.records["issues"] == 3
        assert report.records["wiki_versions"] == 3

    async def test_a_zip_of_it_is_valid_too(self, tmp_path):
        archive = _zip(tmp_path / "full.zip", _bundle_entries(FULL))
        report = await _validate(archive)
        assert report.ok, report.format_text()

    async def test_only_the_manifest_is_required(self, tmp_path):
        root = tmp_path / "b"
        root.mkdir()
        (root / "manifest.json").write_text(json.dumps(manifest()))
        assert (await _validate(root)).ok

    async def test_the_identifier_map_is_scoped_by_the_manifests_source(self):
        adapter = BundleSourceAdapter(FULL)
        # "bundle" names the source on the command line; the rows go under the
        # system and instance the manifest names.
        assert adapter.source_system == "bundle"
        assert adapter.id_map_system == "sample"
        assert adapter.source_instance == "tracker.example.com"
        assert adapter.reference_style == REFERENCE_STYLE_PLACEHOLDERS

    def test_source_instance_can_be_overridden(self):
        adapter = BundleSourceAdapter(FULL, source_instance="legacy")
        assert (adapter.id_map_system, adapter.source_instance) == ("sample", "legacy")

    @pytest.mark.parametrize("system", ["Redmine", "red mine", "red-mine", "redmine/../x", "r" * 31, ""])
    async def test_the_source_system_is_a_strict_slug(self, tmp_path, system):
        """It becomes part of the import account's login, so nothing else gets through."""
        root = write_bundle(tmp_path / "b", manifest_data=manifest(source={"system": system, "instance": "x"}))
        assert _codes(await _validate(root)) == [("manifest.json", None, "schema")]

    async def test_bundle_is_not_a_source_system_a_manifest_may_name(self, tmp_path):
        root = write_bundle(tmp_path / "b", manifest_data=manifest(source={"system": "bundle", "instance": "x"}))
        report = await _validate(root)
        assert _codes(report) == [("manifest.json", None, "schema")]
        assert "reserved" in report.errors[0].message


class TestVersions:
    async def test_an_unknown_major_version_is_refused(self, tmp_path):
        root = write_bundle(tmp_path / "b", manifest_data=manifest(format_version="2.0"), users="not json at all\n")
        report = await _validate(root)
        # Refused on the manifest alone: nothing else is read.
        assert _codes(report) == [("manifest.json", None, "unsupported_version")]
        assert "version 1.x" in report.problems[0].message

    async def test_connect_refuses_it_before_anything_is_loaded(self, tmp_path):
        root = write_bundle(tmp_path / "b", manifest_data=manifest(format_version="2.0"))
        with pytest.raises(BundleInvalidError, match="not supported"):
            await BundleSourceAdapter(root).connect()

    async def test_a_newer_minor_version_loads_with_its_new_fields_ignored(self, tmp_path):
        root = write_bundle(
            tmp_path / "b",
            manifest_data=manifest(format_version="1.4"),
            users=[user("u1", pronouns="they/them")],
        )
        report = await _validate(root)
        assert report.ok
        assert ("users.jsonl", 1, "schema") in _codes(report, Severity.WARNING)

    async def test_an_unknown_field_is_an_error_in_the_same_minor_version(self, tmp_path):
        root = write_bundle(tmp_path / "b", users=[user("u1", pronouns="they/them")])
        assert _codes(await _validate(root)) == [("users.jsonl", 1, "schema")]

    async def test_something_that_is_not_a_bundle(self, tmp_path):
        root = write_bundle(tmp_path / "b", manifest_data=manifest(format="other-format"))
        assert _codes(await _validate(root)) == [("manifest.json", None, "unsupported_version")]

    async def test_no_manifest(self, tmp_path):
        (tmp_path / "b").mkdir()
        assert _codes(await _validate(tmp_path / "b")) == [("manifest.json", None, "missing_file")]

    async def test_a_path_that_is_neither_a_directory_nor_a_zip(self, tmp_path):
        (tmp_path / "bundle.tar").write_bytes(b"not an archive")
        assert _codes(await _validate(tmp_path / "bundle.tar")) == [("bundle.tar", None, "unreadable")]


class TestRecords:
    async def test_invalid_json_names_its_line(self, tmp_path):
        root = write_bundle(tmp_path / "b", users=json.dumps(user("u1")) + "\n{oops\n")
        assert _codes(await _validate(root)) == [("users.jsonl", 2, "invalid_json")]

    async def test_a_schema_error_names_the_line_and_the_property(self, tmp_path):
        root = write_bundle(
            tmp_path / "b",
            projects=[project("p1")],
            issues=[issue("i1"), issue("i2", created_at="2024-02-01 10:00")],
        )
        report = await _validate(root)
        assert _codes(report) == [("issues.jsonl", 2, "schema")]
        assert report.problems[0].message.startswith("/created_at:")

    async def test_a_timestamp_without_an_offset_is_rejected(self, tmp_path):
        root = write_bundle(tmp_path / "b", users=[user("u1", created_at="2024-02-01T10:00:00")])
        assert _codes(await _validate(root)) == [("users.jsonl", 1, "schema")]

    async def test_a_missing_required_property(self, tmp_path):
        record = issue("i1")
        del record["subject"]
        root = write_bundle(tmp_path / "b", projects=[project("p1")], issues=[record])
        report = await _validate(root)
        assert _codes(report) == [("issues.jsonl", 1, "schema")]
        assert "'subject' is a required property" in report.problems[0].message

    async def test_a_source_ref_with_whitespace_is_rejected(self, tmp_path):
        root = write_bundle(tmp_path / "b", users=[user("has space")])
        assert _codes(await _validate(root)) == [("users.jsonl", 1, "schema")]

    async def test_a_record_file_that_is_not_a_file(self, tmp_path):
        root = write_bundle(tmp_path / "b")
        (root / "issues.jsonl").mkdir()
        assert _codes(await _validate(root)) == [("issues.jsonl", None, "unreadable")]

    async def test_blank_lines_are_ignored(self, tmp_path):
        root = write_bundle(tmp_path / "b", users="\n" + json.dumps(user("u1")) + "\n\n")
        report = await _validate(root)
        assert report.ok and report.records["users"] == 1


class TestReferences:
    async def test_an_unresolved_ref_names_its_line_and_field(self, tmp_path):
        root = write_bundle(tmp_path / "b", projects=[project("p1")], issues=[issue("i1", author_ref="ghost")])
        report = await _validate(root)
        assert _codes(report) == [("issues.jsonl", 1, "unresolved_ref")]
        assert "author_ref: no user with source_ref 'ghost'" in report.problems[0].message

    async def test_a_parent_further_down_the_file_resolves(self, tmp_path):
        root = write_bundle(
            tmp_path / "b", projects=[project("p1")], issues=[issue("child", parent_ref="parent"), issue("parent")]
        )
        assert (await _validate(root)).ok

    async def test_a_missing_parent_is_reported(self, tmp_path):
        root = write_bundle(tmp_path / "b", projects=[project("p1")], issues=[issue("child", parent_ref="parent")])
        assert _codes(await _validate(root)) == [("issues.jsonl", 1, "unresolved_ref")]

    async def test_a_lookup_ref_is_checked(self, tmp_path):
        root = write_bundle(tmp_path / "b", projects=[project("p1")], issues=[issue("i1", status_ref="closed")])
        assert _codes(await _validate(root)) == [("issues.jsonl", 1, "unresolved_ref")]

    async def test_a_reference_inside_lookups_gives_a_pointer(self, tmp_path):
        lookups = {"trackers": [{"source_ref": "bug", "name": "Bug", "default_status_ref": "nope"}]}
        report = await _validate(write_bundle(tmp_path / "b", lookups=lookups))
        assert [(p.file, p.pointer, str(p.code)) for p in report.errors] == [
            ("lookups.json", "/trackers/0/default_status_ref", "unresolved_ref")
        ]

    async def test_a_duplicate_ref_names_both_lines(self, tmp_path):
        root = write_bundle(tmp_path / "b", users=[user("u1"), user("u2"), user("u1")])
        report = await _validate(root)
        assert _codes(report) == [("users.jsonl", 3, "duplicate_ref")]
        assert "(first at line 1)" in report.problems[0].message

    async def test_the_same_ref_in_two_entity_types_is_fine(self, tmp_path):
        root = write_bundle(tmp_path / "b", users=[user("1")], projects=[project("1")], issues=[issue("1", "1")])
        assert (await _validate(root)).ok

    async def test_a_placeholder_naming_a_missing_record(self, tmp_path):
        root = write_bundle(
            tmp_path / "b",
            projects=[project("p1")],
            issues=[issue("i1", description="See {{issue:i9}} and {{wiki:w1}}.")],
        )
        report = await _validate(root)
        # An issue may be in an earlier bundle of the same source; any other
        # record must be in this one.
        assert _codes(report) == [("issues.jsonl", 1, "unresolved_ref")]
        assert report.errors[0].message.startswith("description {{wiki:…}}: no wiki_page")
        assert _codes(report, Severity.WARNING) == [("issues.jsonl", None, "external_ref")]

    async def test_an_issue_placeholder_outside_the_bundle_is_a_warning(self, tmp_path):
        """The load looks it up among the source's earlier imports, which validation cannot see."""
        root = write_bundle(
            tmp_path / "b",
            projects=[project("p1")],
            issues=[
                issue("i1", description="Follows {{issue:a-7}} and {{issue:i2}}."),
                issue("i2", description="Also {{issue:a-7|the old bug}}, {{issue:a-8}}."),
            ],
            journals=[{"source_ref": "j1", "issue_ref": "i1", "notes": "See {{issue:a-9}}."}],
            wiki_pages=[
                {
                    "source_ref": "w1",
                    "project_ref": "p1",
                    "title": "Home",
                    "versions": [{"source_ref": "wv1", "version": 1, "text": "{{issue:a-7}}"}],
                }
            ],
        )
        report = await _validate(root)
        assert report.ok, report.format_text()
        assert [(p.file, p.line, p.pointer, str(p.code)) for p in report.warnings] == [
            ("issues.jsonl", None, None, "external_ref"),
            ("journals.jsonl", None, None, "external_ref"),
            ("wiki_pages.jsonl", None, None, "external_ref"),
        ]
        assert [p.message for p in report.warnings] == [
            "3 issue placeholder(s) name no issue in this bundle, e.g. 'a-7' (line 1), 'a-8' (line 2); "
            "loading looks them up in earlier imports of the same source and writes any it cannot find as plain text",
            "1 issue placeholder(s) name no issue in this bundle, e.g. 'a-9' (line 1); "
            "loading looks them up in earlier imports of the same source and writes any it cannot find as plain text",
            "1 issue placeholder(s) name no issue in this bundle, e.g. 'a-7' (line 1); "
            "loading looks them up in earlier imports of the same source and writes any it cannot find as plain text",
        ]

    async def test_an_outside_issue_warning_gives_at_most_five_examples(self, tmp_path):
        text = " ".join(f"{{{{issue:x-{n}}}}}" for n in range(7))
        root = write_bundle(tmp_path / "b", projects=[project("p1")], issues=[issue("i1", description=text)])
        (warning,) = (await _validate(root)).warnings
        assert warning.message.startswith(
            "7 issue placeholder(s) name no issue in this bundle, e.g. 'x-0' (line 1), 'x-1' (line 1), "
            "'x-2' (line 1), 'x-3' (line 1), 'x-4' (line 1); "
        )

    async def test_a_placeholder_may_name_a_record_in_a_later_file(self, tmp_path):
        data = b"bytes"
        root = write_bundle(
            tmp_path / "b",
            projects=[project("p1")],
            issues=[issue("i1", description="[log]({{attachment:a1}}) {{wiki:w1}}")],
            wiki_pages=[{"source_ref": "w1", "project_ref": "p1", "title": "Home"}],
            attachments=[
                {
                    "source_ref": "a1",
                    "container_kind": "issue",
                    "container_ref": "i1",
                    "filename": "log.txt",
                    "sha256": stored(data),
                }
            ],
            files={stored(data): data},
        )
        assert (await _validate(root)).ok

    async def test_a_placeholder_in_code_is_not_checked(self, tmp_path):
        root = write_bundle(
            tmp_path / "b", projects=[project("p1")], issues=[issue("i1", description="```\n{{issue:nope}}\n```")]
        )
        assert (await _validate(root)).ok

    async def test_a_user_placeholder_takes_no_label(self, tmp_path):
        root = write_bundle(
            tmp_path / "b",
            users=[user("u1")],
            projects=[project("p1")],
            issues=[issue("i1", description="{{user:u1|Alex}}")],
        )
        assert _codes(await _validate(root)) == [("issues.jsonl", 1, "invalid_ref")]

    async def test_an_issue_assigned_to_a_group_is_a_warning(self, tmp_path):
        root = write_bundle(
            tmp_path / "b",
            groups=[{"source_ref": "g1", "name": "Team"}],
            projects=[project("p1")],
            issues=[issue("i1", assigned_to_ref="g1")],
        )
        report = await _validate(root)
        assert report.ok
        assert _codes(report, Severity.WARNING) == [("issues.jsonl", 1, "invalid_ref")]

    async def test_a_custom_value_must_use_its_fields_key(self, tmp_path):
        field = {"source_ref": "cf1", "name": "Severity", "key": "severity", "field_format": "list"}
        value = {"field_ref": "cf1", "key": "sev", "value": "High"}
        root = write_bundle(
            tmp_path / "b", projects=[project("p1")], custom_fields=[field], issues=[issue("i1", custom_values=[value])]
        )
        assert _codes(await _validate(root)) == [("issues.jsonl", 1, "invalid_ref")]

    async def test_two_custom_fields_cannot_share_a_key(self, tmp_path):
        fields = [
            {"source_ref": "cf1", "name": "Severity", "key": "severity", "field_format": "list"},
            {"source_ref": "cf2", "name": "Severity (old)", "key": "severity", "field_format": "list"},
        ]
        assert _codes(await _validate(write_bundle(tmp_path / "b", custom_fields=fields))) == [
            ("custom_fields.jsonl", 2, "invalid_ref")
        ]

    async def test_a_user_ref_custom_value_is_checked(self, tmp_path):
        field = {"source_ref": "cf1", "name": "Reviewer", "key": "reviewer", "field_format": "user"}
        value = {"field_ref": "cf1", "key": "reviewer", "value": "ghost", "value_kind": "user_ref"}
        root = write_bundle(
            tmp_path / "b", projects=[project("p1")], custom_fields=[field], issues=[issue("i1", custom_values=[value])]
        )
        assert _codes(await _validate(root)) == [("issues.jsonl", 1, "unresolved_ref")]

    async def test_a_parent_cycle(self, tmp_path):
        root = write_bundle(tmp_path / "b", projects=[project("a", parent_ref="b"), project("b", parent_ref="a")])
        assert _codes(await _validate(root)) == [("projects.jsonl", None, "parent_cycle")]

    async def test_an_issue_cannot_relate_to_itself(self, tmp_path):
        relation = {"source_ref": "r1", "from_ref": "i1", "to_ref": "i1", "relation_type": "relates"}
        root = write_bundle(tmp_path / "b", projects=[project("p1")], issues=[issue("i1")], relations=[relation])
        assert _codes(await _validate(root)) == [("relations.jsonl", 1, "invalid_ref")]

    async def test_wiki_revisions_must_be_oldest_first(self, tmp_path):
        page = {
            "source_ref": "w1",
            "project_ref": "p1",
            "title": "Home",
            "versions": [
                {"source_ref": "v2", "version": 2, "text": "new"},
                {"source_ref": "v1", "version": 1, "text": "old"},
            ],
        }
        root = write_bundle(tmp_path / "b", projects=[project("p1")], wiki_pages=[page])
        assert _codes(await _validate(root)) == [("wiki_pages.jsonl", 1, "schema")]


class TestFiles:
    def _attachment(self, sha256: str, **fields) -> dict:
        return {
            "source_ref": "a1",
            "container_kind": "issue",
            "container_ref": "i1",
            "filename": "log.txt",
            "sha256": sha256,
            **fields,
        }

    def _bundle(self, root: Path, attachment: dict, files: dict[str, bytes] | None) -> Path:
        return write_bundle(root, projects=[project("p1")], issues=[issue("i1")], attachments=[attachment], files=files)

    async def test_a_missing_file(self, tmp_path):
        root = self._bundle(tmp_path / "b", self._attachment(stored(b"x")), None)
        assert _codes(await _validate(root)) == [("attachments.jsonl", 1, "missing_file")]

    async def test_a_file_whose_bytes_do_not_match_its_name(self, tmp_path):
        root = self._bundle(tmp_path / "b", self._attachment(stored(b"x")), {stored(b"x"): b"tampered"})
        assert _codes(await _validate(root)) == [("attachments.jsonl", 1, "hash_mismatch")]

    async def test_a_wrong_size_is_only_a_warning(self, tmp_path):
        root = self._bundle(tmp_path / "b", self._attachment(stored(b"x"), filesize=99), {stored(b"x"): b"x"})
        report = await _validate(root)
        assert report.ok
        assert _codes(report, Severity.WARNING) == [("attachments.jsonl", 1, "size_mismatch")]

    async def test_an_unreferenced_file_is_a_warning(self, tmp_path):
        files = {stored(b"x"): b"x", stored(b"y"): b"y"}
        report = await _validate(self._bundle(tmp_path / "b", self._attachment(stored(b"x")), files))
        assert report.ok
        assert _codes(report, Severity.WARNING) == [(f"files/{stored(b'y')}", None, "unreferenced_file")]

    async def test_a_symlink_in_files_is_refused(self, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"server secret")
        root = self._bundle(tmp_path / "b", self._attachment(stored(b"server secret")), None)
        (root / "files").mkdir()
        os.symlink(secret, root / "files" / stored(b"server secret"))
        report = await _validate(root)
        assert _codes(report) == [(f"files/{stored(b'server secret')}", None, "symlink")]
        # Refused outright: no record was read.
        assert report.records == {}

    async def test_a_symlinked_record_file_is_refused(self, tmp_path):
        elsewhere = tmp_path / "users.jsonl"
        elsewhere.write_text(json.dumps(user("u1")) + "\n")
        root = write_bundle(tmp_path / "b")
        os.symlink(elsewhere, root / "users.jsonl")
        assert _codes(await _validate(root)) == [("users.jsonl", None, "symlink")]

    async def test_unknown_files_are_ignored_with_a_warning(self, tmp_path):
        root = write_bundle(tmp_path / "b")
        (root / "README.txt").write_text("hello")
        report = await _validate(root)
        assert report.ok
        assert _codes(report, Severity.WARNING) == [("README.txt", None, "unknown_file")]


class TestASeparateFilesDirectory:
    """``--files-path``: the records come from the bundle, the bytes from a directory of their own."""

    def _attachment(self, sha256: str, **fields) -> dict:
        return {
            "source_ref": "a1",
            "container_kind": "issue",
            "container_ref": "i1",
            "filename": "log.txt",
            "sha256": sha256,
            **fields,
        }

    def _data(self, root: Path, attachment: dict) -> Path:
        return write_bundle(root, projects=[project("p1")], issues=[issue("i1")], attachments=[attachment])

    def _files(self, root: Path, files: dict[str, bytes]) -> Path:
        root.mkdir(parents=True)
        for name, data in files.items():
            (root / name).write_bytes(data)
        return root

    async def _validate(self, bundle: Path, files: Path) -> ValidationReport:
        return await BundleSourceAdapter(bundle, files_path=files).validate()

    async def test_a_zipped_data_only_bundle_reads_the_bytes_from_the_directory(self, tmp_path):
        data = {name: body for name, body in _bundle_entries(FULL).items() if not name.startswith("files/")}
        archive = _zip(tmp_path / "data.zip", data)
        adapter = BundleSourceAdapter(archive, files_path=FULL / "files")
        await adapter.connect()
        try:
            attachments = [a async for a in adapter.extract_attachments("p-web")]
            paths = {a.filename: adapter.resolve_attachment_path(a) for a in attachments}
        finally:
            await adapter.close()
        assert paths["deploy.log"] == (FULL / "files" / SHA_LOG).resolve()
        assert paths["deploy.log"].read_bytes().startswith(b"2024-02-01T10:00:00Z deploy started")

    async def test_a_valid_split_bundle_reports_nothing(self, tmp_path):
        bundle = self._data(tmp_path / "b", self._attachment(stored(b"x"), filesize=1))
        files = self._files(tmp_path / "store", {stored(b"x"): b"x"})
        report = await self._validate(bundle, files)
        assert report.ok, report.format_text()
        assert report.problems == []

    async def test_a_missing_file_names_the_record_line(self, tmp_path):
        bundle = self._data(tmp_path / "b", self._attachment(stored(b"x")))
        report = await self._validate(bundle, self._files(tmp_path / "store", {}))
        assert _codes(report) == [("attachments.jsonl", 1, "missing_file")]
        assert report.errors[0].message == f"files/{stored(b'x')} is not in the bundle"

    async def test_a_wrong_hash_names_the_record_line(self, tmp_path):
        bundle = self._data(tmp_path / "b", self._attachment(stored(b"x")))
        report = await self._validate(bundle, self._files(tmp_path / "store", {stored(b"x"): b"tampered"}))
        assert _codes(report) == [("attachments.jsonl", 1, "hash_mismatch")]
        assert report.errors[0].message == f"files/{stored(b'x')} does not hash to its name"

    async def test_a_wrong_size_is_only_a_warning(self, tmp_path):
        bundle = self._data(tmp_path / "b", self._attachment(stored(b"x"), filesize=99))
        report = await self._validate(bundle, self._files(tmp_path / "store", {stored(b"x"): b"x"}))
        assert report.ok
        assert _codes(report, Severity.WARNING) == [("attachments.jsonl", 1, "size_mismatch")]

    async def test_a_symlink_in_the_directory_is_refused_and_no_record_is_read(self, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"server secret")
        bundle = self._data(tmp_path / "b", self._attachment(stored(b"server secret")))
        files = self._files(tmp_path / "store", {})
        os.symlink(secret, files / stored(b"server secret"))
        report = await self._validate(bundle, files)
        assert _codes(report) == [(f"files/{stored(b'server secret')}", None, "symlink")]
        assert report.errors[0].message == "Symbolic links are not allowed in a bundle"
        assert report.records == {}

    async def test_a_symlink_further_down_is_refused_too(self, tmp_path):
        bundle = self._data(tmp_path / "b", self._attachment(stored(b"x")))
        files = self._files(tmp_path / "store", {stored(b"x"): b"x"})
        (files / "nested").mkdir()
        os.symlink(tmp_path / "secret.txt", files / "nested" / "link")
        report = await self._validate(bundle, files)
        assert _codes(report) == [("files/nested/link", None, "symlink")]

    async def test_the_directory_itself_may_be_a_link(self, tmp_path):
        bundle = self._data(tmp_path / "b", self._attachment(stored(b"x")))
        files = self._files(tmp_path / "store", {stored(b"x"): b"x"})
        os.symlink(files, tmp_path / "current")
        report = await self._validate(bundle, tmp_path / "current")
        assert report.ok, report.format_text()
        assert report.problems == []

    async def test_other_entries_are_ignored_and_unused_files_are_warned_about(self, tmp_path):
        bundle = self._data(tmp_path / "b", self._attachment(stored(b"x")))
        files = self._files(tmp_path / "store", {stored(b"x"): b"x", stored(b"y"): b"y", "README.txt": b"hi"})
        report = await self._validate(bundle, files)
        assert report.ok
        assert _codes(report, Severity.WARNING) == [
            ("files/README.txt", None, "unknown_file"),
            (f"files/{stored(b'y')}", None, "unreferenced_file"),
        ]
        assert report.warnings[0].message == "Not a stored file (files/<sha256>); ignored"

    @pytest.mark.parametrize("kind", ["missing", "file"])
    async def test_a_directory_that_is_not_there_is_refused_before_the_scan(self, tmp_path, kind):
        bundle = self._data(tmp_path / "b", self._attachment(stored(b"x")))
        files = tmp_path / "store"
        if kind == "file":
            files.write_bytes(b"not a directory")
        report = await self._validate(bundle, files)
        assert _codes(report) == [("store", None, "unreadable")]
        assert report.errors[0].message == "The files directory does not exist or is not a directory"
        assert report.records == {}

    async def test_the_bundles_own_files_are_ignored_with_a_warning(self, tmp_path):
        bundle = write_bundle(
            tmp_path / "b",
            projects=[project("p1")],
            issues=[issue("i1")],
            attachments=[self._attachment(stored(b"x"))],
            files={stored(b"x"): b"stale", "junk": b"j"},
        )
        files = self._files(tmp_path / "store", {stored(b"x"): b"x"})
        report = await self._validate(bundle, files)
        assert report.ok, report.format_text()
        assert [(p.file, str(p.code), p.message) for p in report.warnings] == [
            (
                "files",
                "unknown_file",
                "Stored files are read from the files directory given; the bundle's own are ignored",
            )
        ]

    async def test_a_zips_own_files_are_ignored_with_a_warning_and_not_unpacked(self, tmp_path):
        archive = _zip(tmp_path / "full.zip", _bundle_entries(FULL))
        adapter = BundleSourceAdapter(archive, files_path=FULL / "files")
        await adapter.connect()
        try:
            assert not (adapter.index.root / "files").exists()
            assert [(p.file, str(p.code)) for p in adapter.report.warnings] == [("files", "unknown_file")]
        finally:
            await adapter.close()

    @pytest.mark.parametrize("where", ["inside", "around"])
    async def test_the_directory_cannot_be_inside_the_bundle_or_hold_it(self, tmp_path, where):
        if where == "inside":
            bundle = self._data(tmp_path / "b", self._attachment(stored(b"x")))
            files = self._files(bundle / "store", {stored(b"x"): b"x"})
        else:
            files = self._files(tmp_path / "store", {stored(b"x"): b"x"})
            bundle = self._data(files / "b", self._attachment(stored(b"x")))
        report = await self._validate(bundle, files)
        assert _codes(report) == [("store", None, "unreadable")]
        assert report.errors[0].message == "The files directory must not be inside the bundle or contain it"
        assert report.records == {}


class TestZipSafety:
    def _entries(self, tmp_path: Path) -> dict[str, bytes]:
        return _bundle_entries(write_bundle(tmp_path / "src", users=[user("u1")]))

    @pytest.mark.parametrize(
        "name",
        ["../escape.txt", "files/../../escape.txt", "/etc/cron.d/escape", "C:/escape.txt", "files\\..\\escape.txt"],
    )
    async def test_a_path_leaving_the_bundle_refuses_the_archive(self, tmp_path, name):
        archive = _zip(tmp_path / "b.zip", {**self._entries(tmp_path), name: b"payload"})
        report = await _validate(archive)
        assert _codes(report) == [(name, None, "unsafe_entry")]
        assert report.records == {}
        assert not (tmp_path / "escape.txt").exists()

    async def test_a_symlink_entry_refuses_the_archive(self, tmp_path):
        archive = _zip(tmp_path / "b.zip", self._entries(tmp_path), links={"files/" + "a" * 64: "/etc/passwd"})
        report = await _validate(archive)
        assert _codes(report) == [("files/" + "a" * 64, None, "symlink")]
        assert report.records == {}

    async def test_a_duplicate_entry_refuses_the_archive(self, tmp_path):
        archive = tmp_path / "b.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            for name, data in self._entries(tmp_path).items():
                handle.writestr(name, data)
            with pytest.warns(UserWarning):
                handle.writestr("users.jsonl", json.dumps(user("evil")) + "\n")
        assert ("users.jsonl", None, "unsafe_entry") in _codes(await _validate(archive))

    async def test_the_unpacked_copy_is_removed_on_close(self, tmp_path):
        adapter = BundleSourceAdapter(_zip(tmp_path / "b.zip", self._entries(tmp_path)))
        await adapter.connect()
        root = adapter.index.root
        assert (root / "users.jsonl").exists()
        await adapter.close()
        assert not root.exists()


class TestCounts:
    async def test_a_count_that_does_not_match(self, tmp_path):
        root = write_bundle(tmp_path / "b", manifest_data=manifest(counts={"users": 3}), users=[user("u1")])
        report = await _validate(root)
        assert [(p.file, p.pointer, str(p.code)) for p in report.errors] == [
            ("manifest.json", "/counts/users", "count_mismatch")
        ]

    async def test_a_count_for_an_absent_file_must_be_zero(self, tmp_path):
        root = write_bundle(tmp_path / "b", manifest_data=manifest(counts={"issues": 0, "users": 0}))
        assert (await _validate(root)).ok


class TestTheAdapterReadsBack:
    async def _connected(self, root: Path) -> BundleSourceAdapter:
        adapter = BundleSourceAdapter(root)
        await adapter.connect()
        return adapter

    async def test_projects_come_parents_first(self):
        adapter = await self._connected(FULL)
        try:
            projects = [p async for p in adapter.extract_projects()]
        finally:
            await adapter.close()
        assert all(isinstance(p, IRProject) for p in projects)
        assert [p.source_ref for p in projects] == ["p-web", "p-api"]

    async def test_issues_come_parents_first_within_their_project(self):
        adapter = await self._connected(FULL)
        try:
            web = [i.source_ref async for i in adapter.extract_issues("p-web")]
            api = [i.source_ref async for i in adapter.extract_issues("p-api")]
        finally:
            await adapter.close()
        assert web == ["web-1", "web-2"]
        assert api == ["api-1"]

    async def test_journals_come_oldest_first(self):
        adapter = await self._connected(FULL)
        try:
            journals = [j.source_ref async for j in adapter.extract_journals("p-web")]
        finally:
            await adapter.close()
        assert journals == ["j-1", "j-2"]

    async def test_records_decode_to_ir(self):
        adapter = await self._connected(FULL)
        try:
            users = [u async for u in adapter.extract_users()]
            issues = {i.source_ref: i async for i in adapter.extract_issues("p-web")}
            attachments = [a async for a in adapter.extract_attachments("p-web")]
            lookups = await adapter.extract_lookups()
            access = await adapter.extract_public_access()
            redirects = [r async for r in adapter.extract_wiki_redirects("p-web")]
            wiki_watchers = [w async for w in adapter.extract_wiki_watchers("p-web")]
        finally:
            await adapter.close()

        assert all(isinstance(u, IRUser) for u in users)
        assert isinstance(issues["web-1"], IRIssue)
        assert str(issues["web-2"].estimated_hours) == "2.5"
        assert issues["web-1"].custom_values[1].value == "u-carol"
        assert {a.source_ref for a in attachments} == {"a-log", "a-notes"}
        assert all(isinstance(a, IRAttachment) for a in attachments)
        assert [s.name for s in lookups.statuses] == ["Triage", "In Review", "Shipped"]
        assert access is not None and access.anonymous_permissions == {"p-web": ["view_issues", "view_wiki"]}
        assert [(r.title, r.redirects_to) for r in redirects] == [("Start", "Home")]
        assert [(w.container_ref, w.user_ref) for w in wiki_watchers] == [("w-home", "u-bob")]

    async def test_an_attachment_resolves_to_its_stored_file(self):
        adapter = await self._connected(FULL)
        try:
            (attachment,) = [a async for a in adapter.extract_attachments("p-web") if a.source_ref == "a-log"]
            path = adapter.resolve_attachment_path(attachment)
            assert path.read_bytes().startswith(b"2024-02-01T10:00:00Z deploy started")
            attachment.storage_key = "../../etc/passwd"
            with pytest.raises(ValueError):
                adapter.resolve_attachment_path(attachment)
        finally:
            await adapter.close()

    async def test_without_public_access_there_is_nothing_to_say(self, tmp_path):
        adapter = await self._connected(write_bundle(tmp_path / "b"))
        try:
            assert await adapter.extract_public_access() is None
        finally:
            await adapter.close()

    async def test_notes_for_the_report(self, tmp_path):
        adapter = await self._connected(copy_full(tmp_path / "b"))
        try:
            notes = list(adapter.source_notes())
        finally:
            await adapter.close()
        assert ("left_behind_by_the_exporter", "Sprint boards have no equivalent (2)") in notes
        assert ("left_behind_by_the_exporter", "attachment a-forum: Attachment on a forum post") in notes
        assert ("records_with_source_data_kept_only_in_the_bundle", "issues: 1") in notes

    async def test_passwords_algorithms_come_from_the_bundle(self, tmp_path):
        credential = {"algorithm": "acme_md5", "fields": {"digest": "x"}}
        adapter = await self._connected(write_bundle(tmp_path / "b", users=[user("u1", credential=credential)]))
        try:
            assert adapter.credential_algorithms == ("acme_md5",)
        finally:
            await adapter.close()


class TestRefsAndHistoryText:
    async def test_a_source_ref_ending_in_a_newline(self, tmp_path):
        """The schema's pattern lets it through under Python, so the reader checks it."""
        report = await _validate(write_bundle(tmp_path / "b", users=[user("u1\n")]))
        assert _codes(report) == [("users.jsonl", 1, "schema")]
        assert "ends in a newline" in report.problems[0].message

    async def test_a_lookup_ref_ending_in_a_newline(self, tmp_path):
        lookups = {"priorities": [{"source_ref": "n\n", "name": "Normal"}]}
        report = await _validate(write_bundle(tmp_path / "b", lookups=lookups))
        assert [(p.file, p.pointer, str(p.code)) for p in report.errors] == [
            ("lookups.json", "/priorities/0", "schema")
        ]

    async def test_placeholders_in_a_description_change_are_checked(self, tmp_path):
        journal = {
            "source_ref": "j1",
            "issue_ref": "i1",
            "details": [
                {
                    "property": "attr",
                    "prop_key": "description",
                    "old_value": "{{issue:i1}}",
                    "new_value": "{{user:ghost}}",
                },
                # Any other field's values are stored as written.
                {"property": "attr", "prop_key": "subject", "new_value": "{{user:ghost}}"},
            ],
        }
        root = write_bundle(tmp_path / "b", projects=[project("p1")], issues=[issue("i1")], journals=[journal])
        report = await _validate(root)
        assert _codes(report) == [("journals.jsonl", 1, "unresolved_ref")]
        assert report.problems[0].message.startswith("details.new_value {{user:…}}: no user")
