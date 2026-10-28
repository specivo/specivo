"""Loading an import bundle end to end.

The bundle is ``tests/fixtures/import_bundle/full``: hand-written, so these
tests check the format as documented rather than what the writer happens to
produce. It uses every file of the format, so a full load touches every loader.

Marked ``serial`` for the same reason as the Redmine imports: a load writes
instance-wide rows (roles, statuses, activities, accounts) that other tests
insert too.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from specivo.importers.bundle import BundleInvalidError
from specivo.importers.core.ir import EntityType
from specivo.models.attachment import Attachment
from specivo.models.import_id_map import ImportIdMapping
from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.lookups import IssueCategory, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.relation import IssueRelation
from specivo.models.role import Role
from specivo.models.time_entry import TimeEntry
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.models.version import Version
from specivo.models.watcher import Watcher
from specivo.models.wiki import WikiContent, WikiPage, WikiRedirect
from tests.fixtures.import_bundle import FULL, copy_full, issue, user, write_bundle

pytestmark = [
    pytest.mark.asyncio(loop_scope="function"),
    pytest.mark.integration,
    pytest.mark.serial,
]

SHA_LOG = "bb184ab3bffbeb05ed23843ec1ffebc22065b8f6ef3d9c11e4fd5cc0f043be44"
SHA_NOTES = "6bceefb7c7e1eaed129f5d3a374d751a1a6d68a6bd46508aeeb78e3f4cd0fe3d"

COUNTED = (
    Project,
    Issue,
    Journal,
    WikiPage,
    WikiContent,
    Attachment,
    TimeEntry,
    IssueRelation,
    User,
    Watcher,
    Member,
    MemberRole,
)


async def _count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def _issue(session, subject: str) -> Issue:
    return (await session.execute(select(Issue).where(Issue.subject == subject))).scalar_one()


async def _user(session, login: str) -> User:
    return (await session.execute(select(User).where(User.login == login))).scalar_one()


async def _page(session, title: str) -> WikiPage:
    return (await session.execute(select(WikiPage).where(WikiPage.title == title))).scalar_one()


async def _counts(session) -> dict[str, int]:
    return {model.__name__: await _count(session, model) for model in COUNTED}


class TestFullBundle:
    @pytest.fixture
    async def imported(self, db_session, build_bundle_import):
        return await build_bundle_import(FULL).run()

    async def test_the_run_reports_no_warnings(self, imported):
        assert [w.message for w in imported.warnings] == []

    async def test_every_record_is_created(self, imported):
        created = imported.created
        assert created[EntityType.USER] == 3
        assert created[EntityType.GROUP] == 1
        assert created[EntityType.PROJECT] == 2
        assert created[EntityType.ISSUE] == 3
        assert created[EntityType.JOURNAL] == 3
        assert created[EntityType.RELATION] == 1
        assert created[EntityType.WIKI_PAGE] == 2
        assert created[EntityType.ATTACHMENT] == 3
        assert created[EntityType.TIME_ENTRY] == 2
        assert created[EntityType.MEMBERSHIP] == 3

    async def test_lookups(self, db_session, imported):
        statuses = {s.name: s.category for s in (await db_session.execute(select(IssueStatus))).scalars()}
        assert {"Triage": "backlog", "In Review": "active", "Shipped": "closed"}.items() <= statuses.items()
        chore = (await db_session.execute(select(Tracker).where(Tracker.name == "Chore"))).scalar_one()
        assert chore.is_in_roadmap is False
        assert chore.disabled_core_fields == ["estimated_hours"]

    async def test_users_keep_their_identity(self, db_session, imported):
        alice = await _user(db_session, "bundle_alice")
        assert alice.email == "bundle_alice@example.com"
        assert alice.display_name == "Alice Example"
        assert alice.created_at == datetime(2023, 1, 10, 1, 0, tzinfo=UTC)
        assert (await _user(db_session, "bundle_bob")).is_admin is True
        carol = await _user(db_session, "bundle_carol")
        assert carol.status == "locked"
        # No address in the source: a placeholder in the reserved domain.
        assert carol.email == "bundle_carol@invalid"

    async def test_the_group_and_memberships(self, db_session, imported):
        group = (await db_session.execute(select(UserGroup).where(UserGroup.name == "Bundle Core Team"))).scalar_one()
        members = set(
            (
                await db_session.execute(
                    select(User.login)
                    .join(UserGroupMember, UserGroupMember.user_id == User.id)
                    .where(UserGroupMember.group_id == group.id)
                )
            ).scalars()
        )
        assert members == {"bundle_alice", "bundle_bob"}

        web = (await db_session.execute(select(Project).where(Project.identifier == "bundle-web"))).scalar_one()
        held = (
            await db_session.execute(
                select(Role.name)
                .join(MemberRole, MemberRole.role_id == Role.id)
                .join(Member, Member.id == MemberRole.member_id)
                .where(Member.project_id == web.id, Member.group_id == group.id)
            )
        ).scalars()
        assert set(held) == {"Contributor"}

    async def test_projects_with_the_child_listed_first(self, db_session, imported):
        projects = {p.identifier: p for p in (await db_session.execute(select(Project))).scalars()}
        web, api = projects["bundle-web"], projects["bundle-api"]
        assert api.parent_id == web.id
        assert web.is_public is True
        assert web.key == "BUNDLEWEB"

    async def test_issues_are_numbered_parents_first(self, db_session, imported):
        parent = await _issue(db_session, "Login fails after the session times out")
        child = await _issue(db_session, "Session cookie is not cleared")
        assert (parent.project_key, parent.sequence_number) == ("BUNDLEWEB", 1)
        assert (child.project_key, child.sequence_number) == ("BUNDLEWEB", 2)
        assert child.parent_id == parent.id

    async def test_issue_fields(self, db_session, imported):
        child = await _issue(db_session, "Session cookie is not cleared")
        assert child.estimated_hours == Decimal("2.5")
        assert child.done_ratio == 50
        assert (child.start_date, child.due_date) == (date(2024, 2, 1), date(2024, 2, 15))

        issue = await _issue(db_session, "Login fails after the session times out")
        # A parent's progress and dates come from its children, as always in Specivo.
        assert (issue.done_ratio, issue.start_date) == (50, date(2024, 2, 1))
        assert issue.created_at == datetime(2024, 2, 1, 10, 0, tzinfo=UTC)
        assert (await db_session.get(User, issue.author_id)).login == "bundle_bob"
        assert (await db_session.get(User, issue.assigned_to_id)).login == "bundle_alice"
        assert (await db_session.get(IssueCategory, issue.category_id)).name == "Frontend"
        assert (await db_session.get(Version, issue.fixed_version_id)).name == "1.0"
        assert (await db_session.get(IssueStatus, issue.status_id)).name == "In Review"

        private = await _issue(db_session, "Rate limit headers are missing")
        assert private.is_private is True
        assert private.closed_on == datetime(2024, 2, 20, 10, 0, tzinfo=UTC)

    async def test_a_user_custom_value_is_the_users_specivo_id(self, db_session, imported):
        issue = await _issue(db_session, "Login fails after the session times out")
        carol = await _user(db_session, "bundle_carol")
        assert issue.issue_metadata == {"severity": "High", "reviewer": carol.id}

    async def test_placeholders_in_a_description_are_expanded(self, db_session, imported):
        issue = await _issue(db_session, "Login fails after the session times out")
        assert issue.description == (
            "Blocks BUNDLEWEB-2; see [[Home]].\n\nReported by @bundle_bob, log in [deploy.log](deploy.log)."
        )

    async def test_labelled_and_cross_project_placeholders_become_links(self, db_session, imported):
        issue = await _issue(db_session, "Rate limit headers are missing")
        assert "[the login bug](/issue/BUNDLEWEB-1/)" in issue.description
        assert "[the runbook](/projects/BUNDLEWEB/wiki/home/)" in issue.description

    async def test_a_placeholder_in_code_is_left_as_written(self, db_session, imported):
        issue = await _issue(db_session, "Rate limit headers are missing")
        assert "```\n{{issue:web-1}} stays as written in code\n```" in issue.description

    async def test_history_in_order_with_authors_and_dates(self, db_session, imported):
        issue = await _issue(db_session, "Login fails after the session times out")
        journals = (
            (await db_session.execute(select(Journal).where(Journal.issue_id == issue.id).order_by(Journal.sequence)))
            .scalars()
            .all()
        )
        # The first is the baseline the issue's creation records.
        imported_entries = journals[1:]
        assert [(await db_session.get(User, j.user_id)).login for j in imported_entries] == [
            "bundle_bob",
            "bundle_alice",
        ]
        assert [j.created_at for j in imported_entries] == [
            datetime(2024, 2, 2, 10, 0, tzinfo=UTC),
            datetime(2024, 2, 6, 10, 0, tzinfo=UTC),
        ]
        assert imported_entries[0].is_private is True
        assert imported_entries[1].notes == "Reproduced on staging, @bundle_alice is on it."
        detail = (
            await db_session.execute(select(JournalDetail).where(JournalDetail.journal_id == imported_entries[1].id))
        ).scalar_one()
        assert (detail.property, detail.prop_key, detail.old_value, detail.new_value) == (
            "attr",
            "status_id",
            "triage",
            "review",
        )

    async def test_a_relation_across_projects(self, db_session, imported):
        relation = (await db_session.execute(select(IssueRelation))).scalar_one()
        assert relation.relation_type == "blocks"
        assert (await db_session.get(Issue, relation.issue_from_id)).subject.startswith("Login fails")
        assert (await db_session.get(Issue, relation.issue_to_id)).subject.startswith("Rate limit")

    async def test_wiki_history_renumbered_with_placeholders_expanded(self, db_session, imported):
        home = await _page(db_session, "Home")
        assert home.protected is True
        revisions = (
            (
                await db_session.execute(
                    select(WikiContent).where(WikiContent.page_id == home.id).order_by(WikiContent.version)
                )
            )
            .scalars()
            .all()
        )
        assert [r.version for r in revisions] == [1, 2]
        assert revisions[1].comments == "Link the runbook"
        assert revisions[1].created_at == datetime(2024, 1, 25, 10, 0, tzinfo=UTC)
        assert revisions[1].text == "# Home\n\nSee [[Operations]], BUNDLEWEB-1 and [the notes](notes.txt)."

        operations = await _page(db_session, "Operations")
        assert operations.parent_id == home.id
        text = (
            await db_session.execute(select(WikiContent.text).where(WikiContent.page_id == operations.id))
        ).scalar_one()
        assert text == "# Operations\n\nOn call: @bundle_bob."

    async def test_the_redirect(self, db_session, imported):
        redirect = (await db_session.execute(select(WikiRedirect))).scalar_one()
        assert (redirect.title_from, redirect.redirected_to) == ("start", "home")

    async def test_attachments_arrive_with_their_bytes(self, db_session, imported, build_bundle_import):
        attachments = {a.filename: a for a in (await db_session.execute(select(Attachment))).scalars()}
        assert set(attachments) == {"deploy.log", "deploy copy.log", "notes.txt"}
        assert attachments["deploy.log"].content_hash == SHA_LOG
        assert attachments["deploy copy.log"].content_hash == SHA_LOG
        assert attachments["notes.txt"].content_hash == SHA_NOTES
        assert attachments["notes.txt"].container_type == "WikiPage"
        stored = build_bundle_import.storage / attachments["deploy.log"].disk_filename
        assert stored.read_bytes().startswith(b"2024-02-01T10:00:00Z deploy started")

    async def test_watchers(self, db_session, imported):
        issue = await _issue(db_session, "Login fails after the session times out")
        carol = await _user(db_session, "bundle_carol")
        bob = await _user(db_session, "bundle_bob")
        home = await _page(db_session, "Home")
        watching = set(
            (await db_session.execute(select(Watcher.user_id).where(Watcher.issue_id == issue.id))).scalars()
        )
        assert carol.id in watching
        wiki = set((await db_session.execute(select(Watcher.user_id).where(Watcher.wiki_page_id == home.id))).scalars())
        assert wiki == {bob.id}

    async def test_time_entries(self, db_session, imported):
        hours = sorted((await db_session.execute(select(TimeEntry.hours))).scalars())
        assert hours == [Decimal("0.75"), Decimal("1.50")]

    async def test_the_version_ends_up_locked(self, db_session, imported):
        version = (await db_session.execute(select(Version).where(Version.name == "1.0"))).scalar_one()
        assert (version.status, version.sharing) == ("locked", "descendants")

    async def test_public_access_is_opted_in_with_the_switch_left_off(self, db_session, imported):
        web = (await db_session.execute(select(Project).where(Project.identifier == "bundle-web"))).scalar_one()
        assert sorted(web.anonymous_permissions) == ["view_issues", "view_wiki"]
        assert "Visitor role: view_boards" in imported.notes["permissions_not_carried_over"]

    async def test_the_report_says_what_the_bundle_left_behind(self, imported):
        assert imported.notes["left_behind_by_the_exporter"] == [
            "Sprint boards have no equivalent (2)",
            "attachment a-forum: Attachment on a forum post",
        ]
        assert imported.notes["records_with_source_data_kept_only_in_the_bundle"] == ["issues: 1"]

    async def test_the_identifier_map_is_scoped_by_the_bundles_source(self, db_session, imported):
        scopes = set(
            (await db_session.execute(select(ImportIdMapping.source_system, ImportIdMapping.source_instance))).all()
        )
        assert scopes == {("bundle", "sample:tracker.example.com")}


class TestDryRun:
    async def test_nothing_is_written(self, db_session, build_bundle_import):
        summary = await build_bundle_import(FULL, dry_run=True).run()

        assert summary.created[EntityType.ISSUE] == 3
        await db_session.rollback()
        for model in (Project, Issue, WikiPage, Attachment, ImportIdMapping):
            assert await _count(db_session, model) == 0, model.__name__
        assert list(build_bundle_import.storage.iterdir()) == []


class TestRerun:
    async def test_the_same_bundle_twice_creates_nothing_new(self, db_session, build_bundle_import):
        await build_bundle_import(FULL).run()
        before = await _counts(db_session)

        second = await build_bundle_import(FULL).run()

        assert await _counts(db_session) == before
        assert sum(second.created.values()) == 0
        assert second.skipped[EntityType.ISSUE] == 3
        assert second.warnings == []

    async def test_a_later_bundle_adds_only_what_is_new(self, db_session, build_bundle_import, tmp_path):
        """A delta run: same source, same refs, one more issue."""
        await build_bundle_import(FULL).run()

        later = copy_full(tmp_path / "later")
        with (later / "issues.jsonl").open("a") as handle:
            new = issue("web-3", "p-web", tracker_ref="defect", status_ref="triage", priority_ref="routine")
            new["description"] = "Follow-up to {{issue:web-1}}."
            handle.write(json.dumps(new) + "\n")
        manifest = json.loads((later / "manifest.json").read_text())
        manifest["counts"]["issues"] = 4
        (later / "manifest.json").write_text(json.dumps(manifest))

        second = await build_bundle_import(later).run()

        assert second.created[EntityType.ISSUE] == 1
        assert second.skipped[EntityType.ISSUE] == 3
        added = await _issue(db_session, "Issue web-3")
        assert (added.project_key, added.sequence_number) == ("BUNDLEWEB", 3)
        assert added.description == "Follow-up to BUNDLEWEB-1."


class TestInvalidBundle:
    async def test_an_invalid_bundle_is_refused_before_anything_is_written(
        self, db_session, build_bundle_import, tmp_path
    ):
        root = write_bundle(tmp_path / "bad", users=[user("u1"), user("u1")])
        users_before = await _count(db_session, User)

        with pytest.raises(BundleInvalidError, match=r"users\.jsonl:2"):
            await build_bundle_import(root).run()

        assert await _count(db_session, User) == users_before
        assert await _count(db_session, ImportIdMapping) == 0
