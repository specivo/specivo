"""Service tests for issues, history, relations and watchers.

The cases that matter are the ones a naive importer gets wrong: a subtask
whose parent has a higher id than it does, history that has to keep its order
and its dates, a relation Specivo refuses that Redmine allowed, and custom
field values naming a user who is imported later.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from specivo.importers.core.ir import (
    ContainerKind,
    EntityType,
    IRCustomField,
    IRCustomValue,
    IRIssue,
    IRJournalDetail,
    IRJournalEntry,
    IRLookups,
    IRPriority,
    IRProject,
    IRRelation,
    IRStatus,
    IRTracker,
    IRUser,
    IRWatcher,
    ValueKind,
)
from specivo.importers.load.issue_loader import (
    load_issues,
    load_journals,
    load_relations,
    load_watchers,
    resolve_custom_field_references,
)
from specivo.importers.load.lookup_loader import load_lookups
from specivo.importers.load.project_loader import load_projects
from specivo.importers.load.user_loader import load_users
from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.relation import IssueRelation
from specivo.models.watcher import Watcher
from tests.services.conftest import FakeAdapter

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]


class IssueAdapter(FakeAdapter):
    """Adapter with its own source system.

    The import service account's login is derived from it, and test modules
    run in parallel: two of them inserting the same login in uncommitted
    transactions block on the unique index.
    """

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("source_system", "redmineissue")
        super().__init__(**kwargs)


def _lookups() -> IRLookups:
    return IRLookups(
        statuses=[
            IRStatus(source_ref="1", name="New", category="backlog"),
            IRStatus(source_ref="5", name="Closed", category="closed"),
        ],
        trackers=[IRTracker(source_ref="1", name="Bug", default_status_ref="1")],
        priorities=[IRPriority(source_ref="4", name="Normal", is_default=True)],
    )


def _issue(ref: str = "100", **overrides) -> IRIssue:
    data = {
        "source_ref": ref,
        "project_ref": "1",
        "tracker_ref": "1",
        "status_ref": "1",
        "priority_ref": "4",
        "subject": "Login is broken",
        "author_ref": "7",
        "description": "h2. Steps\n\nIt breaks.",
        "created_at": datetime(2020, 3, 1, 9, 0, tzinfo=UTC),
        "updated_at": datetime(2021, 6, 1, 9, 0, tzinfo=UTC),
    }
    data.update(overrides)
    return IRIssue(**data)


def _adapter(**kwargs) -> FakeAdapter:
    base = {
        "lookups": _lookups(),
        "users": [IRUser(source_ref="7", login="issue_alex", display_name="Alex", email="issue_alex@example.org")],
        "projects": [IRProject(source_ref="1", identifier="issuetest", name="Issue Test")],
    }
    base.update(kwargs)
    return IssueAdapter(**base)


@pytest_asyncio.fixture
async def loaded(make_context):
    """Load lookups, users and projects, then return the shared context."""

    async def _load(adapter: FakeAdapter, **options):
        ctx = make_context(adapter, **options)
        await load_lookups(ctx)
        await load_users(ctx)
        await load_projects(ctx)
        return ctx

    return _load


class TestIssues:
    async def test_creates_the_issue(self, db_session, loaded):
        ctx = await loaded(_adapter(issues=[_issue()]))
        await load_issues(ctx)

        issue_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "100")
        issue = await db_session.get(Issue, issue_id)
        assert issue.subject == "Login is broken"
        assert issue.status_id == await ctx.id_map.get(db_session, EntityType.STATUS, "1")
        assert issue.priority_id == await ctx.id_map.get(db_session, EntityType.PRIORITY, "4")

    async def test_display_key_uses_the_project_key(self, db_session, loaded):
        ctx = await loaded(_adapter(issues=[_issue()]))
        await load_issues(ctx)

        issue_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "100")
        issue = await db_session.get(Issue, issue_id)
        assert issue.display_key == f"ISSUETEST-{issue.sequence_number}"

    async def test_description_markup_is_converted(self, db_session, loaded):
        ctx = await loaded(_adapter(issues=[_issue()]))
        await load_issues(ctx)

        issue_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "100")
        assert (await db_session.get(Issue, issue_id)).description.startswith("## Steps")

    async def test_issue_references_are_left_for_the_later_pass(self, db_session, loaded):
        """A reference can point at an issue in a project imported later."""
        ctx = await loaded(_adapter(issues=[_issue(description="Caused by #42.")]))
        await load_issues(ctx)

        issue_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "100")
        assert "#42" in (await db_session.get(Issue, issue_id)).description

    async def test_original_timestamps_are_restored(self, db_session, loaded):
        ctx = await loaded(_adapter(issues=[_issue()]))
        await load_issues(ctx)

        issue_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "100")
        issue = await db_session.get(Issue, issue_id)
        assert issue.created_at == datetime(2020, 3, 1, 9, 0, tzinfo=UTC)
        assert issue.updated_at == datetime(2021, 6, 1, 9, 0, tzinfo=UTC)

    async def test_closed_date_is_kept(self, db_session, loaded):
        closed = datetime(2021, 7, 1, 9, 0, tzinfo=UTC)
        ctx = await loaded(_adapter(issues=[_issue(status_ref="5", closed_at=closed)]))
        await load_issues(ctx)

        issue_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "100")
        assert (await db_session.get(Issue, issue_id)).closed_on == closed

    async def test_planning_fields_carry_across(self, db_session, loaded):
        ctx = await loaded(
            _adapter(
                issues=[
                    _issue(
                        start_date=date(2020, 3, 1),
                        due_date=date(2020, 4, 1),
                        estimated_hours=Decimal("7.5"),
                        done_ratio=40,
                    )
                ]
            )
        )
        await load_issues(ctx)

        issue_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "100")
        issue = await db_session.get(Issue, issue_id)
        assert issue.start_date == date(2020, 3, 1)
        assert issue.due_date == date(2020, 4, 1)
        assert issue.estimated_hours == Decimal("7.50")
        assert issue.done_ratio == 40

    async def test_private_issue_stays_private(self, db_session, loaded):
        ctx = await loaded(_adapter(issues=[_issue(is_private=True)]))
        await load_issues(ctx)

        issue_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "100")
        assert (await db_session.get(Issue, issue_id)).is_private is True

    async def test_unknown_author_falls_back_to_the_import_account(self, db_session, loaded):
        """Redmine keeps issues whose author was deleted; Specivo needs one."""
        ctx = await loaded(_adapter(issues=[_issue(author_ref="999")]))
        await load_issues(ctx)

        issue_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "100")
        issue = await db_session.get(Issue, issue_id)
        from specivo.models.user import User

        assert (await db_session.get(User, issue.author_id)).is_service_account is True

    async def test_unresolvable_assignee_is_reported(self, db_session, loaded):
        """Redmine can assign to a group, which Specivo cannot express."""
        ctx = await loaded(_adapter(issues=[_issue(assigned_to_ref="999")]))
        await load_issues(ctx)

        issue_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "100")
        assert (await db_session.get(Issue, issue_id)).assigned_to_id is None
        assert any("left unassigned" in w.message for w in ctx.summary.warnings)


class TestHierarchy:
    async def test_child_is_attached_to_its_parent(self, db_session, loaded):
        issues = [_issue("100"), _issue("101", parent_ref="100", subject="Subtask")]
        ctx = await loaded(_adapter(issues=issues))
        await load_issues(ctx)

        parent_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "100")
        child_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "101")
        assert (await db_session.get(Issue, child_id)).parent_id == parent_id

    async def test_nested_set_bounds_are_rebuilt(self, db_session, loaded):
        """Specivo builds its own tree; the source's bounds never line up."""
        issues = [_issue("100"), _issue("101", parent_ref="100", subject="Subtask")]
        ctx = await loaded(_adapter(issues=issues))
        await load_issues(ctx)

        parent = await db_session.get(Issue, await ctx.id_map.get(db_session, EntityType.ISSUE, "100"))
        child = await db_session.get(Issue, await ctx.id_map.get(db_session, EntityType.ISSUE, "101"))
        assert parent.lft < child.lft < child.rgt < parent.rgt

    async def test_parent_with_a_higher_id_still_comes_first(self, db_session, loaded):
        """A subtask can be older than the issue it was later attached to.

        The adapter orders the stream, so the loader sees the parent first even
        though the source lists the child first.
        """
        issues = [_issue("101", parent_ref="200", subject="Subtask"), _issue("200", subject="Parent")]
        ctx = await loaded(_adapter(issues=issues))
        await load_issues(ctx)

        parent_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "200")
        child_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "101")
        assert child_id is not None
        assert (await db_session.get(Issue, child_id)).parent_id == parent_id

    async def test_parent_outside_the_import_becomes_top_level(self, db_session, loaded):
        """A parent in a project that was not selected must not take the child."""
        ctx = await loaded(_adapter(issues=[_issue("101", parent_ref="999", subject="Subtask")]))
        await load_issues(ctx)

        child_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "101")
        assert (await db_session.get(Issue, child_id)).parent_id is None
        assert any("parent was not imported" in w.message for w in ctx.summary.warnings)


class TestCustomFieldValues:
    async def test_scalar_values_are_stored_as_metadata(self, db_session, loaded):
        values = [IRCustomValue(field_ref="1", key="severity", value="High")]
        ctx = await loaded(_adapter(issues=[_issue(custom_values=values)]))
        await load_issues(ctx)

        issue_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "100")
        assert (await db_session.get(Issue, issue_id)).issue_metadata["severity"] == "High"

    async def test_user_references_are_rewritten_to_specivo_ids(self, db_session, loaded):
        """Stored as source ids first, since the user may be imported later."""
        values = [IRCustomValue(field_ref="1", key="reviewer", value=7, value_kind=ValueKind.USER_REF)]
        adapter = _adapter(
            issues=[_issue(custom_values=values)],
            custom_fields=[
                IRCustomField(
                    source_ref="1",
                    name="Reviewer",
                    key="reviewer",
                    field_format="user",
                    json_schema={"type": "integer"},
                )
            ],
        )
        ctx = await loaded(adapter)
        await load_issues(ctx)
        ctx.state["custom_field_keys"] = {field.source_ref: field for field in adapter.custom_fields}
        await resolve_custom_field_references(ctx)

        issue_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "100")
        expected = await ctx.id_map.get(db_session, EntityType.USER, "7")
        assert (await db_session.get(Issue, issue_id)).issue_metadata["reviewer"] == expected

    async def test_multi_value_references_are_rewritten(self, db_session, loaded):
        values = [IRCustomValue(field_ref="1", key="reviewers", value=[7], value_kind=ValueKind.USER_REF)]
        adapter = _adapter(
            issues=[_issue(custom_values=values)],
            custom_fields=[
                IRCustomField(
                    source_ref="1",
                    name="Reviewers",
                    key="reviewers",
                    field_format="user",
                    multiple=True,
                    json_schema={"type": "array", "items": {"type": "integer"}},
                )
            ],
        )
        ctx = await loaded(adapter)
        await load_issues(ctx)
        ctx.state["custom_field_keys"] = {field.source_ref: field for field in adapter.custom_fields}
        await resolve_custom_field_references(ctx)

        issue_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "100")
        expected = await ctx.id_map.get(db_session, EntityType.USER, "7")
        assert (await db_session.get(Issue, issue_id)).issue_metadata["reviewers"] == [expected]


class TestJournals:
    def _journal(self, ref: str = "500", **overrides) -> IRJournalEntry:
        data = {
            "source_ref": ref,
            "issue_ref": "100",
            "user_ref": "7",
            "notes": "Looking into it",
            "created_at": datetime(2020, 4, 1, 10, 0, tzinfo=UTC),
        }
        data.update(overrides)
        return IRJournalEntry(**data)

    async def test_comment_is_created(self, db_session, loaded):
        ctx = await loaded(_adapter(issues=[_issue()], journals=[self._journal()]))
        await load_issues(ctx)
        await load_journals(ctx)

        journal_id = await ctx.id_map.get(db_session, EntityType.JOURNAL, "500")
        journal = await db_session.get(Journal, journal_id)
        assert journal.notes == "Looking into it"
        assert journal.created_at == datetime(2020, 4, 1, 10, 0, tzinfo=UTC)

    async def test_field_changes_are_replayed(self, db_session, loaded):
        journal = self._journal(
            notes=None,
            details=[IRJournalDetail(property="attr", prop_key="status_id", old_value="1", new_value="5")],
        )
        ctx = await loaded(_adapter(issues=[_issue()], journals=[journal]))
        await load_issues(ctx)
        await load_journals(ctx)

        journal_id = await ctx.id_map.get(db_session, EntityType.JOURNAL, "500")
        details = (
            (await db_session.execute(select(JournalDetail).where(JournalDetail.journal_id == journal_id)))
            .scalars()
            .all()
        )
        assert len(details) == 1
        assert details[0].property == "attr"
        assert details[0].prop_key == "status_id"
        assert details[0].old_value == "1"
        assert details[0].new_value == "5"

    async def test_entry_can_carry_both_a_note_and_changes(self, db_session, loaded):
        journal = self._journal(
            details=[IRJournalDetail(property="attr", prop_key="done_ratio", old_value="0", new_value="50")]
        )
        ctx = await loaded(_adapter(issues=[_issue()], journals=[journal]))
        await load_issues(ctx)
        await load_journals(ctx)

        journal_id = await ctx.id_map.get(db_session, EntityType.JOURNAL, "500")
        journal_row = await db_session.get(Journal, journal_id)
        assert journal_row.notes == "Looking into it"
        count = (
            await db_session.execute(
                select(func.count()).select_from(JournalDetail).where(JournalDetail.journal_id == journal_id)
            )
        ).scalar_one()
        assert count == 1

    async def test_entries_are_numbered_in_order(self, db_session, loaded):
        journals = [
            self._journal("500", created_at=datetime(2020, 4, 1, tzinfo=UTC)),
            self._journal("501", created_at=datetime(2020, 5, 1, tzinfo=UTC)),
        ]
        ctx = await loaded(_adapter(issues=[_issue()], journals=journals))
        await load_issues(ctx)
        await load_journals(ctx)

        first = await db_session.get(Journal, await ctx.id_map.get(db_session, EntityType.JOURNAL, "500"))
        second = await db_session.get(Journal, await ctx.id_map.get(db_session, EntityType.JOURNAL, "501"))
        assert second.sequence == first.sequence + 1

    async def test_numbering_continues_past_the_description_baseline(self, db_session, loaded):
        """Creating an issue with a description already leaves journal 1."""
        ctx = await loaded(_adapter(issues=[_issue()], journals=[self._journal()]))
        await load_issues(ctx)
        await load_journals(ctx)

        journal = await db_session.get(Journal, await ctx.id_map.get(db_session, EntityType.JOURNAL, "500"))
        assert journal.sequence >= 2

    async def test_private_note_stays_private(self, db_session, loaded):
        ctx = await loaded(_adapter(issues=[_issue()], journals=[self._journal(is_private=True)]))
        await load_issues(ctx)
        await load_journals(ctx)

        journal = await db_session.get(Journal, await ctx.id_map.get(db_session, EntityType.JOURNAL, "500"))
        assert journal.is_private is True

    async def test_note_markup_is_converted(self, db_session, loaded):
        ctx = await loaded(_adapter(issues=[_issue()], journals=[self._journal(notes="*urgent*")]))
        await load_issues(ctx)
        await load_journals(ctx)

        journal = await db_session.get(Journal, await ctx.id_map.get(db_session, EntityType.JOURNAL, "500"))
        assert journal.notes == "**urgent**"

    async def test_second_run_replays_nothing(self, db_session, loaded):
        ctx = await loaded(_adapter(issues=[_issue()], journals=[self._journal()]))
        await load_issues(ctx)
        await load_journals(ctx)
        await load_journals(ctx)

        count = (
            await db_session.execute(select(func.count()).select_from(Journal).where(Journal.notes.is_not(None)))
        ).scalar_one()
        assert count == 1


class TestRelations:
    async def test_relation_is_created(self, db_session, loaded):
        issues = [_issue("100"), _issue("101", subject="Other")]
        relations = [IRRelation(source_ref="900", from_ref="100", to_ref="101", relation_type="relates")]
        ctx = await loaded(_adapter(issues=issues, relations=relations))
        await load_issues(ctx)
        await load_relations(ctx)

        relation_id = await ctx.id_map.get(db_session, EntityType.RELATION, "900")
        assert (await db_session.get(IssueRelation, relation_id)).relation_type == "relates"

    async def test_blocks_relation_keeps_its_direction(self, db_session, loaded):
        issues = [_issue("100"), _issue("101", subject="Other")]
        relations = [IRRelation(source_ref="900", from_ref="100", to_ref="101", relation_type="blocks")]
        ctx = await loaded(_adapter(issues=issues, relations=relations))
        await load_issues(ctx)
        await load_relations(ctx)

        relation = await db_session.get(IssueRelation, await ctx.id_map.get(db_session, EntityType.RELATION, "900"))
        assert relation.issue_from_id == await ctx.id_map.get(db_session, EntityType.ISSUE, "100")
        assert relation.issue_to_id == await ctx.id_map.get(db_session, EntityType.ISSUE, "101")

    async def test_precedes_keeps_its_delay(self, db_session, loaded):
        issues = [_issue("100"), _issue("101", subject="Other")]
        relations = [IRRelation(source_ref="900", from_ref="100", to_ref="101", relation_type="precedes", delay=3)]
        ctx = await loaded(_adapter(issues=issues, relations=relations))
        await load_issues(ctx)
        await load_relations(ctx)

        relation = await db_session.get(IssueRelation, await ctx.id_map.get(db_session, EntityType.RELATION, "900"))
        assert relation.delay == 3

    async def test_relation_to_a_missing_issue_is_reported(self, db_session, loaded):
        relations = [IRRelation(source_ref="900", from_ref="100", to_ref="999", relation_type="relates")]
        ctx = await loaded(_adapter(issues=[_issue("100")], relations=relations))
        await load_issues(ctx)
        await load_relations(ctx)

        assert any("was not imported" in w.message for w in ctx.summary.warnings)

    async def test_rejected_relation_does_not_stop_the_import(self, db_session, loaded):
        """Specivo refuses a relation between an issue and its own descendant."""
        issues = [_issue("100"), _issue("101", parent_ref="100", subject="Subtask")]
        relations = [IRRelation(source_ref="900", from_ref="100", to_ref="101", relation_type="blocks")]
        ctx = await loaded(_adapter(issues=issues, relations=relations))
        await load_issues(ctx)
        await load_relations(ctx)

        assert any("rejected" in w.message for w in ctx.summary.warnings)
        count = (await db_session.execute(select(func.count()).select_from(IssueRelation))).scalar_one()
        assert count == 0


class TestWatchers:
    async def test_watcher_is_subscribed(self, db_session, loaded):
        watchers = [IRWatcher(container_kind=ContainerKind.ISSUE, container_ref="100", user_ref="7")]
        ctx = await loaded(_adapter(issues=[_issue(author_ref="999")], watchers=watchers))
        await load_issues(ctx)
        await load_watchers(ctx)

        issue_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "100")
        user_id = await ctx.id_map.get(db_session, EntityType.USER, "7")
        found = (
            await db_session.execute(select(Watcher).where(Watcher.issue_id == issue_id, Watcher.user_id == user_id))
        ).scalar_one_or_none()
        assert found is not None

    async def test_already_watching_is_not_duplicated(self, db_session, loaded):
        """The issue service auto-watches the author, who may also be a watcher."""
        watchers = [IRWatcher(container_kind=ContainerKind.ISSUE, container_ref="100", user_ref="7")]
        ctx = await loaded(_adapter(issues=[_issue(author_ref="7")], watchers=watchers))
        await load_issues(ctx)
        await load_watchers(ctx)

        issue_id = await ctx.id_map.get(db_session, EntityType.ISSUE, "100")
        user_id = await ctx.id_map.get(db_session, EntityType.USER, "7")
        count = (
            await db_session.execute(
                select(func.count())
                .select_from(Watcher)
                .where(Watcher.issue_id == issue_id, Watcher.user_id == user_id)
            )
        ).scalar_one()
        assert count == 1


class TestIdempotency:
    async def test_second_issue_run_creates_nothing(self, db_session, loaded):
        ctx = await loaded(_adapter(issues=[_issue()]))
        await load_issues(ctx)
        await load_issues(ctx)

        count = (await db_session.execute(select(func.count()).select_from(Issue))).scalar_one()
        assert count == 1
        assert ctx.summary.skipped[EntityType.ISSUE] == 1
