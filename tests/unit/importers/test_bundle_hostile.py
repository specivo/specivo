"""A bundle is untrusted input: hostile ones are refused with a report, never a traceback.

Covers the size limits (zip bombs, archives that lie about their size, huge
lines and files), values that match a pattern but do not exist, a reader that
fails half way, and the rules that keep an import from being pointed at the
wrong earlier import.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import zipfile
from pathlib import Path

import pytest

from specivo.cli.import_data import parse_args
from specivo.importers.bundle import BundleInvalidError, BundleSourceAdapter
from specivo.importers.bundle import adapter as adapter_module
from specivo.importers.bundle import container as container_module
from specivo.importers.bundle.limits import GIB, MIB, BundleLimits, parse_size
from specivo.importers.bundle.report import Severity, ValidationReport
from specivo.importers.core.source import build_source_registry
from tests.fixtures.import_bundle import FULL, issue, manifest, project, stored, user, within_seconds, write_bundle

pytestmark = pytest.mark.unit


@pytest.fixture
def private_tmp(tmp_path, monkeypatch) -> Path:
    """Where the reader unpacks zips during the test, so leftovers can be seen."""
    directory = tmp_path / "unpacked"
    directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    return directory


async def _validate(path: Path, limits: BundleLimits | None = None) -> ValidationReport:
    return await BundleSourceAdapter(path, limits=limits).validate()


def _codes(report: ValidationReport, severity: Severity = Severity.ERROR) -> list[tuple[str, int | None, str]]:
    return [(p.file, p.line, str(p.code)) for p in report.problems if p.severity is severity]


def _zip(path: Path, entries: dict[str, bytes], compression: int = zipfile.ZIP_DEFLATED) -> Path:
    with zipfile.ZipFile(path, "w", compression) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def _small_bundle(**extra: bytes) -> dict[str, bytes]:
    return {
        "manifest.json": json.dumps(manifest()).encode(),
        "users.jsonl": (json.dumps(user("u1")) + "\n").encode(),
        **extra,
    }


class TestZipLimits:
    async def test_a_zip_bomb_is_refused_before_anything_is_unpacked(self, tmp_path, private_tmp):
        bomb = _zip(tmp_path / "bomb.zip", _small_bundle(**{"files/" + "0" * 64: b"\0" * (8 * MIB)}))
        report = await _validate(bomb)
        assert _codes(report) == [("files/" + "0" * 64, None, "too_large")]
        assert "zip bomb" in report.problems[0].message
        assert list(private_tmp.iterdir()) == []

    async def test_bytes_are_counted_while_unpacking_not_taken_from_the_headers(
        self, tmp_path, private_tmp, monkeypatch
    ):
        """An archive can understate its sizes; what is written is what counts."""
        monkeypatch.setattr(container_module, "_check_declared_sizes", lambda *args: True)
        bomb = _zip(tmp_path / "bomb.zip", _small_bundle(**{"files/" + "0" * 64: b"\0" * (8 * MIB)}))
        report = await _validate(bomb)
        assert _codes(report) == [("files/" + "0" * 64, None, "too_large")]
        assert list(private_tmp.iterdir()) == []

    async def test_too_many_entries(self, tmp_path, private_tmp):
        archive = _zip(tmp_path / "b.zip", _small_bundle(**{f"files/{i:064x}": b"x" for i in range(5)}))
        report = await _validate(archive, BundleLimits(max_entries=4))
        assert _codes(report) == [("b.zip", None, "too_large")]

    async def test_too_much_in_total(self, tmp_path, private_tmp):
        entries = _small_bundle(**{f"files/{i:064x}": bytes(range(256)) * 40 for i in range(3)})
        archive = _zip(tmp_path / "b.zip", entries, zipfile.ZIP_STORED)
        report = await _validate(archive, BundleLimits(max_unpacked_bytes=20_000))
        assert _codes(report) == [("b.zip", None, "too_large")]

    async def test_too_much_in_total_counted_while_unpacking(self, tmp_path, private_tmp, monkeypatch):
        monkeypatch.setattr(container_module, "_check_declared_sizes", lambda *args: True)
        entries = _small_bundle(**{f"files/{i:064x}": bytes(range(256)) * 40 for i in range(3)})
        archive = _zip(tmp_path / "b.zip", entries, zipfile.ZIP_STORED)
        report = await _validate(archive, BundleLimits(max_unpacked_bytes=20_000))
        assert [code for _, _, code in _codes(report)] == ["too_large"]
        assert list(private_tmp.iterdir()) == []

    async def test_one_entry_too_large(self, tmp_path, private_tmp):
        archive = _zip(tmp_path / "b.zip", _small_bundle(**{"files/" + "1" * 64: bytes(range(256)) * 8}))
        report = await _validate(archive, BundleLimits(max_entry_bytes=1024))
        assert _codes(report) == [("files/" + "1" * 64, None, "too_large")]


class TestRecordLimits:
    async def test_an_oversized_line_is_reported_and_the_rest_still_read(self, tmp_path):
        lines = [json.dumps(user("u1")), json.dumps(user("u2", display_name="x" * 5000)), json.dumps(user("u3"))]
        root = write_bundle(tmp_path / "b", users="\n".join(lines) + "\n")
        report = await _validate(root, BundleLimits(max_line_bytes=1000))
        assert _codes(report) == [("users.jsonl", 2, "too_large")]
        # The lines either side are read normally, at their own line numbers.
        assert report.records["users"] == 2

    async def test_an_oversized_line_without_a_newline(self, tmp_path):
        root = write_bundle(tmp_path / "b", users=json.dumps(user("u1", display_name="x" * 5000)))
        assert _codes(await _validate(root, BundleLimits(max_line_bytes=1000))) == [("users.jsonl", 1, "too_large")]

    async def test_an_oversized_json_file(self, tmp_path):
        root = write_bundle(tmp_path / "b")
        (root / "lookups.json").write_text(json.dumps({"trackers": [], "pad": "x" * 5000}))
        report = await _validate(root, BundleLimits(max_json_bytes=1000))
        assert _codes(report) == [("lookups.json", None, "too_large")]

    async def test_an_oversized_manifest_is_not_even_read_for_the_instance(self, tmp_path):
        root = write_bundle(tmp_path / "b", manifest_data=manifest(padding="x" * 5000))
        adapter = BundleSourceAdapter(root, limits=BundleLimits(max_json_bytes=1000))
        assert adapter.source_instance == "bundle"
        assert _codes(await adapter.validate()) == [("manifest.json", None, "too_large")]


class TestLimitOptions:
    @pytest.mark.parametrize(("text", "size"), [("512", 512), ("16M", 16 * MIB), ("2G", 2 * GIB), ("1GiB", GIB)])
    def test_sizes_take_a_unit(self, text, size):
        assert parse_size(text) == size

    def test_the_defaults_are_the_documented_ones(self):
        args, cls = parse_args(["--source", "bundle", "--path", str(FULL)], build_source_registry())
        assert cls.from_args(args).limits == BundleLimits()
        spec = (Path(__file__).resolve().parents[3] / "docs" / "import-bundle" / "v1.md").read_text()
        for row in ("| 64G | `--max-unpacked-size`", "| 1000000 | `--max-entries`", "| 16G | `--max-entry-size`"):
            assert row in spec
        for row in ("| 200 | `--max-compression-ratio`", "| 16M | `--max-line-size`", "| 64M | `--max-json-size`"):
            assert row in spec

    def test_limits_can_be_raised(self):
        argv = ["--source", "bundle", "--path", str(FULL), "--max-line-size", "64M", "--max-entries", "5"]
        args, cls = parse_args(argv, build_source_registry())
        limits = cls.from_args(args).limits
        assert (limits.max_line_bytes, limits.max_entries) == (64 * MIB, 5)


class TestValues:
    @pytest.mark.parametrize(
        ("stem", "records", "message"),
        [
            ("users", [user("u1", created_at="2024-02-30T10:00:00Z")], "/created_at: '2024-02-30T10:00:00Z'"),
            ("users", [user("u1", last_login_at="2024-02-01T25:00:00+07:00")], "/last_login_at:"),
            ("issues", [issue("i1", start_date="2024-13-01")], "/start_date: '2024-13-01' is not a valid date"),
        ],
    )
    async def test_a_date_that_does_not_exist_is_a_schema_error(self, tmp_path, stem, records, message):
        root = write_bundle(tmp_path / "b", projects=[project("p1")], **{stem: records})
        report = await _validate(root)
        assert _codes(report) == [(f"{stem}.jsonl", 1, "schema")]
        assert report.problems[0].message.startswith(message)

    async def test_a_bad_date_inside_a_nested_record(self, tmp_path):
        page = {
            "source_ref": "w1",
            "project_ref": "p1",
            "title": "Home",
            "versions": [{"source_ref": "v1", "version": 1, "text": "x", "created_at": "2024-02-31T00:00:00Z"}],
        }
        report = await _validate(write_bundle(tmp_path / "b", projects=[project("p1")], wiki_pages=[page]))
        assert _codes(report) == [("wiki_pages.jsonl", 1, "schema")]
        assert report.problems[0].message.startswith("/versions/0/created_at:")

    async def test_a_bad_export_time(self, tmp_path):
        root = write_bundle(tmp_path / "b", manifest_data=manifest(exported_at="2024-02-30T10:00:00Z"))
        assert _codes(await _validate(root)) == [("manifest.json", None, "schema")]

    async def test_nan_is_not_a_number(self, tmp_path):
        entry = '{"source_ref": "t1", "project_ref": "p1", "hours": NaN, "spent_on": "2024-01-01"}\n'
        root = write_bundle(tmp_path / "b", projects=[project("p1")], time_entries=entry)
        assert _codes(await _validate(root)) == [("time_entries.jsonl", 1, "invalid_json")]

    async def test_connect_refuses_a_bad_date_without_a_traceback(self, tmp_path):
        root = write_bundle(tmp_path / "b", users=[user("u1", created_at="2024-02-30T10:00:00Z")])
        with pytest.raises(BundleInvalidError, match=r"users\.jsonl:1"):
            await BundleSourceAdapter(root).connect()


class TestFailures:
    async def test_an_unexpected_error_becomes_a_report_and_the_copy_is_removed(
        self, tmp_path, private_tmp, monkeypatch
    ):
        seen: list[Path] = []

        def explode(root, report, limits=None, files_dir=None):
            seen.append(root)
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(adapter_module, "scan_bundle", explode)
        archive = _zip(tmp_path / "b.zip", _small_bundle())
        adapter = BundleSourceAdapter(archive)

        with pytest.raises(BundleInvalidError, match="disk on fire"):
            await adapter.connect()

        assert _codes(adapter.report) == [("b.zip", None, "unreadable")]
        assert seen and not seen[0].exists()
        assert list(private_tmp.iterdir()) == []

    async def test_validate_removes_the_copy_too(self, tmp_path, private_tmp):
        report = await _validate(_zip(tmp_path / "b.zip", _small_bundle()))
        assert report.ok
        assert list(private_tmp.iterdir()) == []


class TestSource:
    async def test_the_instance_is_required(self, tmp_path):
        """Without one, two installations of the same system would share an identifier map."""
        root = write_bundle(tmp_path / "b", manifest_data=manifest(source={"system": "sample"}))
        report = await _validate(root)
        assert _codes(report) == [("manifest.json", None, "schema")]
        assert "'instance' is a required property" in report.problems[0].message


class TestLabels:
    @pytest.mark.parametrize("label", ["the runbook]]", "x]](javascript:alert(1))"])
    async def test_a_wiki_label_may_not_close_the_link(self, tmp_path, label):
        page = {"source_ref": "w1", "project_ref": "p1", "title": "Home"}
        root = write_bundle(
            tmp_path / "b",
            projects=[project("p1")],
            issues=[issue("i1", description=f"{{{{wiki:w1|{label}}}}}")],
            wiki_pages=[page],
        )
        report = await _validate(root)
        assert _codes(report) == [("issues.jsonl", 1, "invalid_ref")]
        assert "']]'" in report.problems[0].message


def _validate_now(path: Path, files_path: Path | None = None) -> ValidationReport:
    """Validate from a plain thread, so :func:`within_seconds` can time it."""
    return asyncio.run(BundleSourceAdapter(path, files_path=files_path).validate())


class TestEntriesThatAreNotRegularFiles:
    """A pipe, device, socket or directory under a name the reader opens refuses the bundle unopened.

    Opening a FIFO for reading blocks until something writes to it, so a
    bundle could otherwise hang the import; each case must end within 5 s.
    """

    @pytest.mark.parametrize("name", ["issues.jsonl", "manifest.json", "lookups.json"])
    def test_a_fifo_under_a_known_name(self, tmp_path, name):
        root = write_bundle(tmp_path / "b")
        (root / name).unlink(missing_ok=True)
        os.mkfifo(root / name)
        report = within_seconds(5, lambda: _validate_now(root), root / name)
        assert _codes(report) == [(name, None, "unsafe_entry")]
        assert report.records == {}

    def test_a_directory_under_a_known_name(self, tmp_path):
        root = write_bundle(tmp_path / "b")
        (root / "users.jsonl").mkdir()
        report = within_seconds(5, lambda: _validate_now(root))
        assert _codes(report) == [("users.jsonl", None, "unsafe_entry")]
        assert report.records == {}

    def test_a_fifo_in_files(self, tmp_path):
        root = write_bundle(tmp_path / "b", files={stored(b"x"): b"x"})
        fifo = root / "files" / stored(b"y")
        os.mkfifo(fifo)
        report = within_seconds(5, lambda: _validate_now(root), fifo)
        assert _codes(report) == [(f"files/{stored(b'y')}", None, "unsafe_entry")]
        assert report.records == {}

    def test_a_fifo_in_a_separate_files_directory(self, tmp_path):
        root = write_bundle(tmp_path / "b")
        files = tmp_path / "store"
        files.mkdir()
        fifo = files / stored(b"x")
        os.mkfifo(fifo)
        report = within_seconds(5, lambda: _validate_now(root, files), fifo)
        assert _codes(report) == [(f"files/{stored(b'x')}", None, "unsafe_entry")]
        assert report.records == {}

    def test_a_fifo_with_an_unknown_name_is_only_ignored(self, tmp_path):
        root = write_bundle(tmp_path / "b")
        os.mkfifo(root / "notes")
        report = within_seconds(5, lambda: _validate_now(root), root / "notes")
        assert report.ok
        assert _codes(report, Severity.WARNING) == [("notes", None, "unknown_file")]

    def test_a_record_file_swapped_for_a_fifo_after_the_check(self, tmp_path, monkeypatch):
        root = write_bundle(tmp_path / "b", users=[user("u1")])
        check = container_module._check_directory

        def check_then_swap(directory, report, skip_own_files=False):
            safe = check(directory, report, skip_own_files)
            (directory / "users.jsonl").unlink()
            os.mkfifo(directory / "users.jsonl")
            return safe

        monkeypatch.setattr(container_module, "_check_directory", check_then_swap)
        report = within_seconds(5, lambda: _validate_now(root), root / "users.jsonl")
        assert _codes(report) == [("users.jsonl", None, "unreadable")]


class TestOpenRegular:
    def test_a_regular_file_is_read_in_blocking_mode(self, tmp_path):
        path = tmp_path / "data"
        path.write_bytes(b"data")
        with container_module.open_regular(path) as handle:
            assert os.get_blocking(handle.fileno())
            assert handle.read() == b"data"

    def test_a_fifo_is_refused_without_blocking(self, tmp_path):
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)

        def open_it():
            with pytest.raises(OSError, match="Not a regular file"):
                container_module.open_regular(fifo)

        within_seconds(5, open_it, fifo)

    def test_a_directory_is_refused(self, tmp_path):
        with pytest.raises(OSError, match="Not a regular file"):
            container_module.open_regular(tmp_path)

    def test_a_symlink_is_not_followed(self, tmp_path):
        (tmp_path / "target").write_bytes(b"secret")
        os.symlink(tmp_path / "target", tmp_path / "link")
        with pytest.raises(OSError):
            container_module.open_regular(tmp_path / "link")


SECRET_HASH = "$2y$10$Q9mWkzVtH3secretvaluexyzAbCdEfGhIjKlMnOpQrStUvWxYz012"


class TestSecretsStayOutOfTheReport:
    """A credential that fails its schema is reported without its value: the report is printed and logged."""

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
    async def test_a_bad_credential(self, tmp_path, credential):
        root = write_bundle(tmp_path / "b", users=[user("u1", credential=credential)])
        report = await _validate(root)
        assert _codes(report) == [("users.jsonl", 1, "schema")]
        assert [p.message for p in report.problems] == ["/credential: does not match the schema (anyOf)"]
        assert SECRET_HASH not in report.format_text()
        assert SECRET_HASH not in json.dumps(report.as_dict())

    async def test_other_values_are_still_shown(self, tmp_path):
        root = write_bundle(tmp_path / "b", users=[user("u1", status="gone")])
        [problem] = (await _validate(root)).problems
        assert "'gone'" in problem.message
