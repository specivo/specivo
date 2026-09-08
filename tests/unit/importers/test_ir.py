"""Unit tests for the import intermediate representation and its protocols."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from specivo.importers.core.converter import ContentConverter, ConversionContext, PassthroughConverter
from specivo.importers.core.ir import (
    ContainerKind,
    EntityType,
    IRAttachment,
    IRCustomValue,
    IRIssue,
    IRJournalDetail,
    IRJournalEntry,
    IRLookups,
    IRProject,
    IRStatus,
    IRTimeEntry,
    IRUser,
    IRWikiPage,
    IRWikiVersion,
    PrincipalKind,
    ValueKind,
)
from specivo.importers.core.progress import CliProgressReporter, NullProgressReporter, ProgressReporter
from specivo.importers.core.source import SourceAdapterRegistry

pytestmark = pytest.mark.unit


class TestEntityType:
    def test_values_fit_the_id_map_column(self):
        """entity_type is varchar(30); a longer value would fail on insert."""
        for entity in EntityType:
            assert len(entity.value) <= 30, entity

    def test_values_are_unique(self):
        values = [entity.value for entity in EntityType]
        assert len(values) == len(set(values))

    def test_is_a_plain_string(self):
        """StrEnum members go straight into SQL parameters without conversion."""
        assert EntityType.ISSUE == "issue"
        assert f"{EntityType.WIKI_PAGE}" == "wiki_page"


class TestIrDefaults:
    def test_issue_requires_only_identity_and_lookups(self):
        issue = IRIssue(
            source_ref="42",
            project_ref="7",
            tracker_ref="1",
            status_ref="1",
            priority_ref="2",
            subject="Broken login",
        )
        assert issue.done_ratio == 0
        assert issue.is_private is False
        assert issue.custom_values == []
        assert issue.description is None

    def test_mutable_defaults_are_not_shared(self):
        """A dataclass field factory must give each instance its own list."""
        common = {"project_ref": "1", "tracker_ref": "1", "status_ref": "1", "priority_ref": "1"}
        first = IRIssue(source_ref="1", subject="a", **common)
        second = IRIssue(source_ref="2", subject="b", **common)
        first.custom_values.append(IRCustomValue(field_ref="9", key="severity", value="high"))
        assert second.custom_values == []

    def test_lookups_start_empty(self):
        lookups = IRLookups()
        assert lookups.trackers == []
        assert lookups.statuses == []
        assert lookups.priorities == []
        assert lookups.activities == []
        assert lookups.roles == []

    def test_project_defaults_to_active_and_private(self):
        project = IRProject(source_ref="1", identifier="acme", name="Acme")
        assert project.status == 1
        assert project.is_public is False
        assert project.parent_ref is None

    def test_time_entry_keeps_decimal_hours(self):
        """Hours stay Decimal so rounding to the target scale is exact."""
        entry = IRTimeEntry(
            source_ref="1",
            project_ref="1",
            hours=Decimal("1.333333333333333"),
            spent_on=date(2026, 3, 1),
        )
        assert isinstance(entry.hours, Decimal)
        assert round(entry.hours, 2) == Decimal("1.33")

    def test_journal_entry_carries_details(self):
        entry = IRJournalEntry(
            source_ref="5",
            issue_ref="42",
            notes="Reassigned",
            details=[IRJournalDetail(property="attr", prop_key="status_id", old_value="1", new_value="2")],
        )
        assert entry.details[0].property == "attr"
        assert entry.is_private is False

    def test_wiki_page_holds_ordered_history(self):
        page = IRWikiPage(
            source_ref="3",
            project_ref="1",
            title="Home",
            versions=[
                IRWikiVersion(source_ref="10", version=1, text="one"),
                IRWikiVersion(source_ref="11", version=2, text="two"),
            ],
        )
        assert [v.version for v in page.versions] == [1, 2]

    def test_attachment_size_is_advisory(self):
        """filesize may be absent; the loader recomputes it from the bytes."""
        attachment = IRAttachment(
            source_ref="8",
            container_kind=ContainerKind.ISSUE,
            container_ref="42",
            filename="report.pdf",
            storage_key="2026/03/abc123.pdf",
        )
        assert attachment.filesize is None
        assert attachment.content_type is None

    def test_slots_reject_unknown_attributes(self):
        """slots keep a typo from silently becoming a new attribute."""
        user = IRUser(source_ref="1", login="alex", display_name="Alex")
        with pytest.raises(AttributeError):
            user.emial = "typo@example.com"

    def test_enum_kinds_are_strings(self):
        assert ValueKind.USER_REF == "user_ref"
        assert ContainerKind.WIKI_PAGE == "wiki_page"
        assert PrincipalKind.GROUP == "group"

    def test_status_category_is_the_four_way_grouping(self):
        status = IRStatus(source_ref="5", name="Closed", category="closed")
        assert status.category in {"backlog", "active", "done", "closed"}


class TestSourceAdapterRegistry:
    def test_register_and_get(self):
        registry = SourceAdapterRegistry()

        class FakeAdapter:
            source_system = "fake"

        registry.register("fake", FakeAdapter)
        assert registry.get("fake") is FakeAdapter
        assert registry.names() == ["fake"]

    def test_unknown_source_lists_what_is_available(self):
        registry = SourceAdapterRegistry()
        registry.register("redmine", object)
        with pytest.raises(KeyError) as exc:
            registry.get("jira")
        assert "redmine" in str(exc.value)

    def test_empty_registry_reports_none_registered(self):
        registry = SourceAdapterRegistry()
        with pytest.raises(KeyError) as exc:
            registry.get("redmine")
        assert "none" in str(exc.value)

    def test_register_replaces_previous_entry(self):
        registry = SourceAdapterRegistry()
        registry.register("redmine", object)
        registry.register("redmine", dict)
        assert registry.get("redmine") is dict


class TestConverter:
    def test_passthrough_returns_input_unchanged(self):
        converter = PassthroughConverter()
        assert converter.convert("# Already Markdown", ConversionContext()) == "# Already Markdown"

    def test_passthrough_satisfies_the_protocol(self):
        assert isinstance(PassthroughConverter(), ContentConverter)

    def test_context_defaults_to_textile(self):
        ctx = ConversionContext()
        assert ctx.source_format == "textile"
        assert ctx.attachment_filenames == frozenset()


class TestProgressReporters:
    def test_null_reporter_satisfies_the_protocol(self):
        assert isinstance(NullProgressReporter(), ProgressReporter)

    def test_cli_reporter_satisfies_the_protocol(self):
        assert isinstance(CliProgressReporter(), ProgressReporter)

    def test_cli_reporter_counts_items_per_phase(self, caplog):
        reporter = CliProgressReporter(interval_seconds=0.0)
        with caplog.at_level("INFO"):
            reporter.phase_started("issues")
            reporter.item_done("issues", 3)
            reporter.phase_done("issues")
        assert "3" in caplog.text

    def test_cli_reporter_throttles_progress_lines(self, caplog):
        """A long phase must not log one line per row."""
        reporter = CliProgressReporter(interval_seconds=3600.0)
        reporter.phase_started("issues")
        with caplog.at_level("INFO"):
            for _ in range(50):
                reporter.item_done("issues")
        assert "processed" not in caplog.text

    def test_cli_reporter_logs_warning_context(self, caplog):
        reporter = CliProgressReporter()
        with caplog.at_level("WARNING"):
            reporter.warning("File missing", {"attachment": "8"})
        assert "File missing" in caplog.text
        assert "attachment=8" in caplog.text
