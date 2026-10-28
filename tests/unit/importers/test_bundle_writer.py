"""Unit tests for the bundle writer: IR in, a valid bundle out, the same IR back."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from specivo.importers.bundle import BundleSourceAdapter
from specivo.importers.bundle.writer import BundleWriter
from specivo.importers.core.ir import (
    ContainerKind,
    IRAttachment,
    IRIssue,
    IRLookups,
    IRPriority,
    IRProject,
    IRStatus,
    IRTimeEntry,
    IRTracker,
    IRUser,
    IRWikiPage,
    IRWikiVersion,
)

pytestmark = pytest.mark.unit

WHEN = datetime(2024, 2, 1, 10, 0, tzinfo=UTC)


def _write(target, data: bytes = b"log line\n") -> dict:
    """Write a small bundle to *target* and return the IR that went in."""
    written: dict = {}
    with BundleWriter(target, source_system="sample", source_instance="tracker.example.com", exported_at=WHEN) as w:
        lookups = IRLookups(
            trackers=[IRTracker(source_ref="bug", name="Bug", default_status_ref="open")],
            statuses=[IRStatus(source_ref="open", name="Open", category="backlog")],
            priorities=[IRPriority(source_ref="normal", name="Normal", is_default=True)],
        )
        w.write_lookups(lookups)
        written["user"] = IRUser(source_ref="u1", login="alex", display_name="Alex", created_at=WHEN)
        written["project"] = IRProject(source_ref="p1", identifier="web", name="Web")
        written["issue"] = IRIssue(
            source_ref="i1",
            project_ref="p1",
            tracker_ref="bug",
            status_ref="open",
            priority_ref="normal",
            subject="Login fails",
            description="See [the log]({{attachment:a1}}).",
            estimated_hours=Decimal("1.25"),
        )
        written["page"] = IRWikiPage(
            source_ref="w1",
            project_ref="p1",
            title="Home",
            versions=[IRWikiVersion(source_ref="v1", version=1, text="# Home", created_at=WHEN)],
        )
        written["entry"] = IRTimeEntry(
            source_ref="t1", project_ref="p1", hours=Decimal("0.5"), spent_on=date(2024, 2, 1)
        )
        for record in written.values():
            w.add(record)
        sha256 = w.add_bytes(data)
        written["attachment"] = IRAttachment(
            source_ref="a1",
            container_kind=ContainerKind.ISSUE,
            container_ref="i1",
            filename="server.log",
            storage_key=sha256,
        )
        w.add(written["attachment"])
        w.not_carried_over("Boards", count=2)
    return written


class TestBundleWriter:
    @pytest.mark.parametrize("name", ["bundle", "bundle.zip"])
    async def test_what_is_written_validates_and_reads_back(self, tmp_path, name):
        written = _write(tmp_path / name)
        adapter = BundleSourceAdapter(tmp_path / name)
        assert adapter.source_instance == "sample:tracker.example.com"

        await adapter.connect()
        try:
            assert adapter.report.problems == []
            assert [u async for u in adapter.extract_users()] == [written["user"]]
            assert [p async for p in adapter.extract_projects()] == [written["project"]]
            assert [i async for i in adapter.extract_issues("p1")] == [written["issue"]]
            assert [p async for p in adapter.extract_wiki_pages("p1")] == [written["page"]]
            assert [t async for t in adapter.extract_time_entries("p1")] == [written["entry"]]
            (attachment,) = [a async for a in adapter.extract_attachments("p1")]
            assert attachment == written["attachment"]
            assert adapter.resolve_attachment_path(attachment).read_bytes() == b"log line\n"
        finally:
            await adapter.close()

    def test_the_manifest_counts_what_was_written(self, tmp_path):
        _write(tmp_path / "b")
        manifest = json.loads((tmp_path / "b" / "manifest.json").read_text())
        assert manifest["format_version"] == "1.0"
        assert manifest["exported_at"] == "2024-02-01T10:00:00+00:00"
        assert manifest["counts"]["issues"] == 1
        assert manifest["counts"]["wiki_versions"] == 1
        assert manifest["counts"]["trackers"] == 1
        assert manifest["not_carried_over"] == [{"what": "Boards", "count": 2}]

    def test_one_file_per_content(self, tmp_path):
        with BundleWriter(tmp_path / "b", source_system="sample") as writer:
            first = writer.add_bytes(b"same")
            second = writer.add_bytes(b"same")
        assert first == second
        assert len(list((tmp_path / "b" / "files").iterdir())) == 1

    def test_an_attachment_needs_its_file_first(self, tmp_path):
        attachment = IRAttachment(
            source_ref="a1", container_kind=ContainerKind.ISSUE, container_ref="i1", filename="f", storage_key="0" * 64
        )
        with (
            pytest.raises(ValueError, match="add its file first"),
            BundleWriter(tmp_path / "b", source_system="s") as w,
        ):
            w.add(attachment)

    def test_a_target_that_is_not_empty_is_refused(self, tmp_path):
        (tmp_path / "b").mkdir()
        (tmp_path / "b" / "keep.txt").write_text("x")
        with pytest.raises(FileExistsError):
            BundleWriter(tmp_path / "b", source_system="sample")
