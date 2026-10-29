"""Unit tests for IR <-> bundle record conversion and the placeholder grammar."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from specivo.importers.bundle.codec import from_record, to_record
from specivo.importers.bundle.scan import order_parents_first
from specivo.importers.core.ir import (
    ContainerKind,
    IRAttachment,
    IRCredential,
    IRCustomValue,
    IRIssue,
    IRJournalDetail,
    IRJournalEntry,
    IRProject,
    IRTimeEntry,
    IRUser,
    IRWikiPage,
    IRWikiVersion,
    ValueKind,
)
from specivo.importers.core.placeholders import Placeholder, PlaceholderKind, expand_placeholders, find_placeholders

pytestmark = pytest.mark.unit

BANGKOK = timezone(timedelta(hours=7))


class TestRoundTrip:
    @pytest.mark.parametrize(
        "obj",
        [
            IRUser(
                source_ref="7",
                login="alex",
                display_name="Alex",
                created_at=datetime(2024, 1, 2, 3, 4, 5, tzinfo=BANGKOK),
                credential=IRCredential(algorithm="redmine_sha1", fields={"salt": "s", "hashed_password": "h"}),
            ),
            IRProject(source_ref="1", identifier="web", name="Web", modules=["wiki"], status=9),
            IRIssue(
                source_ref="web-1",
                project_ref="1",
                tracker_ref="bug",
                status_ref="open",
                priority_ref="normal",
                subject="Login fails",
                estimated_hours=Decimal("1.333333"),
                start_date=date(2024, 2, 1),
                custom_values=[
                    IRCustomValue(field_ref="cf1", key="reviewer", value="7", value_kind=ValueKind.USER_REF),
                    IRCustomValue(field_ref="cf2", key="tags", value=["ui", "backend"]),
                ],
            ),
            IRJournalEntry(
                source_ref="j1",
                issue_ref="web-1",
                details=[IRJournalDetail(property="attr", prop_key="status_id", old_value="1", new_value=None)],
            ),
            IRWikiPage(
                source_ref="w1",
                project_ref="1",
                title="Home",
                versions=[IRWikiVersion(source_ref="v1", version=1, text="# Home")],
            ),
            IRAttachment(
                source_ref="a1",
                container_kind=ContainerKind.WIKI_PAGE,
                container_ref="w1",
                filename="notes.txt",
                storage_key="0" * 64,
            ),
            IRTimeEntry(source_ref="t1", project_ref="1", hours=Decimal("0.75"), spent_on=date(2024, 2, 6)),
        ],
        ids=lambda obj: type(obj).__name__,
    )
    def test_a_record_reads_back_as_the_ir_it_was_written_from(self, obj):
        assert from_record(type(obj), to_record(obj)) == obj


class TestEncoding:
    def test_none_is_left_out(self):
        record = to_record(IRProject(source_ref="1", identifier="web", name="Web"))
        assert "parent_ref" not in record and "description" not in record

    def test_timestamps_keep_their_offset(self):
        user = IRUser(
            source_ref="1", login="a", display_name="A", created_at=datetime(2024, 1, 2, 3, 4, tzinfo=BANGKOK)
        )
        assert to_record(user)["created_at"] == "2024-01-02T03:04:00+07:00"

    def test_a_naive_timestamp_is_refused(self):
        """A bundle without offsets would make every reader guess the zone."""
        user = IRUser(source_ref="1", login="a", display_name="A", created_at=datetime(2024, 1, 2))
        with pytest.raises(ValueError, match="no UTC offset"):
            to_record(user)

    def test_decimals_are_exact_strings(self):
        entry = IRTimeEntry(source_ref="t", project_ref="p", hours=Decimal("1.333333"), spent_on=date(2024, 1, 1))
        assert to_record(entry)["hours"] == "1.333333"

    def test_the_storage_key_is_written_as_the_hash(self):
        attachment = IRAttachment(
            source_ref="a", container_kind=ContainerKind.ISSUE, container_ref="i", filename="f", storage_key="ab" * 32
        )
        record = to_record(attachment)
        assert record["sha256"] == "ab" * 32 and "storage_key" not in record


class TestDecoding:
    def test_a_json_number_becomes_the_decimal_it_reads_as(self):
        entry = from_record(
            IRTimeEntry, {"source_ref": "t", "project_ref": "p", "hours": 1.1, "spent_on": "2024-01-01"}
        )
        assert entry.hours == Decimal("1.1")

    def test_zulu_time_is_utc(self):
        user = from_record(
            IRUser, {"source_ref": "1", "login": "a", "display_name": "A", "created_at": "2024-01-02T03:04:05Z"}
        )
        assert user.created_at == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)

    def test_extra_is_not_part_of_the_ir(self):
        project = from_record(IRProject, {"source_ref": "1", "identifier": "w", "name": "W", "extra": {"x": 1}})
        assert project == IRProject(source_ref="1", identifier="w", name="W")

    def test_absent_properties_take_the_ir_default(self):
        project = from_record(IRProject, {"source_ref": "1", "identifier": "w", "name": "W"})
        assert project.status == 1 and project.modules == []


class TestPlaceholders:
    def test_every_kind_is_found(self):
        text = "{{issue:web-1}} {{user:u1}} ![x]({{attachment:a1}}) {{wiki:w1|the runbook}}"
        assert list(find_placeholders(text)) == [
            Placeholder(PlaceholderKind.ISSUE, "web-1"),
            Placeholder(PlaceholderKind.USER, "u1"),
            Placeholder(PlaceholderKind.ATTACHMENT, "a1"),
            Placeholder(PlaceholderKind.WIKI, "w1", "the runbook"),
        ]

    def test_other_braces_are_text(self):
        assert list(find_placeholders("{{monospace}} {{ issue:1 }} {{version:1}}")) == []

    def test_fenced_code_is_left_alone(self):
        text = "{{issue:1}}\n\n```\n{{issue:2}}\n```\n\n~~~\n{{user:3}}\n~~~"
        assert [p.ref for p in find_placeholders(text)] == ["1"]

    def test_expansion_replaces_what_resolves_and_writes_the_rest_as_its_ref(self):
        text, unresolved = expand_placeholders(
            "See {{issue:1}} and {{issue:2}}.", lambda p: "ACME-7" if p.ref == "1" else None
        )
        assert text == "See ACME-7 and 2."
        assert unresolved == [Placeholder(PlaceholderKind.ISSUE, "2")]

    def test_an_unresolved_placeholder_with_a_label_becomes_the_label(self):
        """A reader never sees the placeholder syntax: the label, as plain text, or else the ref."""
        text, unresolved = expand_placeholders(
            "{{issue:a-7|the [old] bug}}, {{wiki:w9|the runbook}}, {{wiki:w8}}, {{user:u9}}, ![x]({{attachment:a9}})",
            lambda p: None,
        )
        assert text == "the [old] bug, the runbook, w8, u9, ![x](a9)"
        assert [(p.kind, p.ref, p.label) for p in unresolved] == [
            (PlaceholderKind.ISSUE, "a-7", "the [old] bug"),
            (PlaceholderKind.WIKI, "w9", "the runbook"),
            (PlaceholderKind.WIKI, "w8", None),
            (PlaceholderKind.USER, "u9", None),
            (PlaceholderKind.ATTACHMENT, "a9", None),
        ]

    def test_expansion_skips_fenced_code(self):
        text, _ = expand_placeholders("{{user:1}}\n```\n{{user:1}}\n```", lambda p: "@alex")
        assert text == "@alex\n```\n{{user:1}}\n```"


class TestParentOrdering:
    def test_an_ordered_list_keeps_its_order(self):
        order, cyclic = order_parents_first({"1": None, "2": "1", "3": None, "4": "2"})
        assert order == ["1", "2", "3", "4"] and cyclic == []

    def test_a_child_listed_first_moves_after_its_parent(self):
        order, _ = order_parents_first({"child": "parent", "other": None, "parent": None})
        assert order == ["other", "parent", "child"]

    def test_a_missing_parent_counts_as_none(self):
        order, _ = order_parents_first({"orphan": "gone"})
        assert order == ["orphan"]

    def test_a_cycle_is_reported_not_looped_on(self):
        order, cyclic = order_parents_first({"a": "b", "b": "a", "c": None, "self": "self"})
        assert order == ["c"]
        assert cyclic == ["a", "b", "self"]


class TestPlaceholderLabelsCannotInjectLinks:
    """A label is text: whatever it holds, the expanded link goes where the placeholder said."""

    LABELS = [
        "x](javascript:alert(1))",
        "x\\](javascript:alert(1))",
        "x\\",
        "[nested](javascript:alert(1))",
    ]

    def _render(self, placeholder: Placeholder, target, project_id: int = 1) -> str:
        from specivo.importers.load.placeholder_rewrite import _format
        from specivo.services.markdown_service import render_wiki_markdown

        return str(render_wiki_markdown(_format(placeholder, target, project_id), "WEB"))

    @pytest.mark.parametrize("label", LABELS)
    def test_an_issue_label(self, label):
        from specivo.importers.load.placeholder_rewrite import _Target

        html = self._render(Placeholder(PlaceholderKind.ISSUE, "1", label), _Target(text="WEB-1"))
        assert re.findall(r'href="([^"]*)"', html) == ["/issue/WEB-1/"]
        assert "javascript:" not in html.split(">", 1)[0]

    @pytest.mark.parametrize("label", LABELS)
    @pytest.mark.parametrize("project_id", [1, 2], ids=["same project", "other project"])
    def test_a_wiki_label(self, label, project_id):
        from specivo.importers.load.placeholder_rewrite import _Target

        target = _Target(text="Home", project_id=1, project_key="WEB", slug="home")
        html = self._render(Placeholder(PlaceholderKind.WIKI, "w1", label), target, project_id)
        assert re.findall(r'href="([^"]*)"', html) == ["/projects/WEB/wiki/home/"]

    def test_a_plain_label_stays_a_wiki_link(self):
        from specivo.importers.load.placeholder_rewrite import _format, _Target

        target = _Target(text="Home", project_id=1, project_key="WEB", slug="home")
        assert _format(Placeholder(PlaceholderKind.WIKI, "w1", "the runbook"), target, 1) == "[[Home|the runbook]]"
