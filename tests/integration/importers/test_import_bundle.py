"""Loading an import bundle end to end.

The bundle is ``tests/fixtures/import_bundle/full``: hand-written, so these
tests check the format as documented rather than what the writer happens to
produce. It uses every file of the format, so a full load touches every loader.

Marked ``serial`` because a load writes
instance-wide rows (roles, statuses, activities, accounts) that other tests
insert too.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from specivo.cli.import_data import parse_args, run_validation
from specivo.importers.bundle import BundleInvalidError
from specivo.importers.core.id_map import ImportIdMap
from specivo.importers.core.ir import EntityType
from specivo.importers.core.links_file import LinksStore, PendingText, links_signing_key, text_sha256
from specivo.importers.core.pipeline import PhaseFailedError
from specivo.importers.core.source import build_source_registry
from specivo.importers.load.pending_links import resolve_links_file
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
from tests.fixtures.import_bundle import FULL, copy_full, issue, manifest, project, stored, user, write_bundle

pytestmark = [
    pytest.mark.asyncio(loop_scope="function"),
    pytest.mark.integration,
    pytest.mark.serial,
]

# How the import summary starts its warning about placeholders it could not expand.
PLACEHOLDERS_LEFT = "Placeholders written as plain text"

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


async def _texts(session) -> dict[str, list]:
    """Every imported text with its timestamp."""
    return {
        "issues": sorted((await session.execute(select(Issue.id, Issue.description, Issue.updated_at))).all()),
        "journals": sorted((await session.execute(select(Journal.id, Journal.notes, Journal.updated_at))).all()),
        "wiki": sorted((await session.execute(select(WikiContent.id, WikiContent.text, WikiContent.updated_at))).all()),
    }


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
        # The source's administrator, imported as an ordinary account by default.
        assert (await _user(db_session, "bundle_bob")).is_admin is False
        carol = await _user(db_session, "bundle_carol")
        assert carol.status == "locked"
        # No address in the source: a placeholder in the reserved domain.
        assert carol.email == "bundle_carol@invalid"

    async def test_accounts_to_review_are_listed(self, imported):
        assert imported.notes["source_administrators_imported_without_administrator_rights"] == [
            "bundle_bob (re-run with --import-admins to keep the rights)"
        ]
        assert imported.notes["accounts_that_signed_in_through_an_external_directory"] == ["bundle_carol"]
        assert imported.notes["accounts_imported_locked_or_not_active"] == ["bundle_carol (locked)"]

    async def test_placeholder_expansion_keeps_the_bundles_timestamps(self, db_session, imported):
        """Expanding a reference is not an edit: updated_at stays what the bundle said."""
        issue = await _issue(db_session, "Login fails after the session times out")
        assert issue.updated_at == datetime(2024, 2, 10, 9, 0, tzinfo=UTC)
        comment = (
            await db_session.execute(select(Journal).where(Journal.notes.like("Reproduced on staging%")))
        ).scalar_one()
        assert comment.updated_at == datetime(2024, 2, 6, 10, 0, tzinfo=UTC)
        home = await _page(db_session, "Home")
        revision = (
            await db_session.execute(
                select(WikiContent).where(WikiContent.page_id == home.id, WikiContent.version == 2)
            )
        ).scalar_one()
        assert "{{" not in revision.text
        assert revision.updated_at == datetime(2024, 1, 25, 10, 0, tzinfo=UTC)

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
        assert child.remaining_estimate == Decimal("1.25")
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

    async def test_bundle_rows_are_scoped_by_the_manifest_source(self, db_session, imported):
        """Keyed as an import straight from that system would be, so the two can meet."""
        scopes = set(
            (await db_session.execute(select(ImportIdMapping.source_system, ImportIdMapping.source_instance))).all()
        )
        assert scopes == {("sample", "tracker.example.com")}
        assert (imported.source_system, imported.source_instance) == ("sample", "tracker.example.com")

    async def test_the_import_account_is_named_after_the_manifests_system(self, db_session, imported):
        accounts = set((await db_session.execute(select(User.login).where(User.login.like("%-import")))).scalars())
        assert accounts == {"sample-import"}


class TestAdministrators:
    async def test_with_import_admins_the_sources_administrator_is_one(self, db_session, build_bundle_import):
        summary = await build_bundle_import(FULL, import_admins=True).run()
        assert (await _user(db_session, "bundle_bob")).is_admin is True
        assert (await _user(db_session, "bundle_alice")).is_admin is False
        assert summary.notes["accounts_imported_as_administrators"] == ["bundle_bob"]
        assert "source_administrators_imported_without_administrator_rights" not in summary.notes


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

        second = await build_bundle_import(FULL, continue_import=True).run()

        assert await _counts(db_session) == before
        assert sum(second.created.values()) == 0
        assert second.skipped[EntityType.ISSUE] == 3
        (warning,) = second.warnings
        assert warning.message.startswith("This source was imported into this database before")
        assert warning.context["source"] == "sample:tracker.example.com"

    @pytest.mark.parametrize("dry_run", [False, True])
    async def test_adding_to_an_earlier_import_needs_the_operators_say_so(
        self, db_session, build_bundle_import, dry_run
    ):
        """A bundle names its own scope, so it could claim another import's records; that takes an opt-in."""
        await build_bundle_import(FULL).run()
        before = await _counts(db_session)
        mapped = await _count(db_session, ImportIdMapping)

        with pytest.raises(PhaseFailedError) as refused:
            await build_bundle_import(FULL, dry_run=dry_run).run()

        message = str(refused.value)
        assert "sample @ tracker.example.com" in message
        assert f"{mapped} identifier-map rows" in message
        assert "3 issue" in message
        assert "--continue-import" in message
        await db_session.rollback()
        assert await _counts(db_session) == before

    async def test_resuming_a_run_is_adding_to_it(self, db_session, build_bundle_import):
        first = await build_bundle_import(FULL, stop_after_phase="users").run()

        resumed = await build_bundle_import(FULL, resume_run_id=first.run_id).run()

        assert resumed.created[EntityType.ISSUE] == 3

    async def test_rerunning_leaves_text_and_timestamps_as_they_were(self, db_session, build_bundle_import):
        await build_bundle_import(FULL).run()
        before = await _texts(db_session)

        await build_bundle_import(FULL, continue_import=True).run()

        assert await _texts(db_session) == before

    async def test_a_placeholder_whose_record_is_not_imported_becomes_its_label(self, db_session, build_bundle_import):
        """Importing only the API project writes its references into the website as their labels,
        as plain text; importing the website later turns them into links."""
        first = await build_bundle_import(FULL, project="p-api").run()
        rate_limits = await _issue(db_session, "Rate limit headers are missing")
        stamped = rate_limits.updated_at
        expected = (
            "Found while fixing the login bug; background in the runbook.\n\n"
            "```\n{{issue:web-1}} stays as written in code\n```"
        )
        assert rate_limits.description == expected
        (left,) = [w for w in first.warnings if w.message.startswith(PLACEHOLDERS_LEFT)]
        assert left.context["count"] == 2
        assert set(left.context["examples"].split(", ")) == {"issue:web-1", "wiki:w-home"}

        await build_bundle_import(FULL, continue_import=True).run()

        await db_session.refresh(rate_limits)
        home = await _page(db_session, "Home")
        assert rate_limits.description == (
            "Found while fixing [the login bug](/issue/BUNDLEWEB-1/); "
            f"background in [the runbook](/projects/BUNDLEWEB/wiki/{home.slug}/).\n\n"
            "```\n{{issue:web-1}} stays as written in code\n```"
        )
        assert rate_limits.updated_at == stamped
        assert not build_bundle_import.links_path(FULL).exists()

    async def test_a_different_instance_is_a_different_import(self, db_session, build_bundle_import, tmp_path):
        """Same system, other installation: nothing is mistaken for already imported."""
        await build_bundle_import(FULL).run()
        other = copy_full(tmp_path / "other")
        manifest = json.loads((other / "manifest.json").read_text())
        manifest["source"]["instance"] = "other.example.com"
        (other / "manifest.json").write_text(json.dumps(manifest))
        # Logins and identifiers are unique, so the second copy is renamed; the
        # point here is only that it is not skipped.
        second = await build_bundle_import(other).run()

        assert second.created[EntityType.ISSUE] == 3
        assert not any(w.message.startswith("This source was imported") for w in second.warnings)

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

        second = await build_bundle_import(later, continue_import=True).run()

        assert second.created[EntityType.ISSUE] == 1
        assert second.skipped[EntityType.ISSUE] == 3
        added = await _issue(db_session, "Issue web-3")
        assert (added.project_key, added.sequence_number) == ("BUNDLEWEB", 3)
        assert added.description == "Follow-up to BUNDLEWEB-1."

    async def test_a_later_bundle_adds_a_new_revision_of_an_existing_page(
        self, db_session, build_bundle_import, tmp_path
    ):
        """A page edited between two exports gets the new revision, after the ones it has."""
        await build_bundle_import(FULL).run()
        home = await _page(db_session, "Home")

        later = copy_full(tmp_path / "later")
        pages = [json.loads(line) for line in (later / "wiki_pages.jsonl").read_text().splitlines()]
        for page in pages:
            if page["source_ref"] == "w-home":
                page["versions"].append(
                    {
                        "source_ref": "wv-9",
                        "version": 4,
                        "text": "# Home\n\nThird draft.",
                        "comments": "Edited after the first export",
                        "author_ref": "u-alice",
                        "created_at": "2024-02-01T10:00:00Z",
                    }
                )
        (later / "wiki_pages.jsonl").write_text("".join(json.dumps(page) + "\n" for page in pages))
        manifest = json.loads((later / "manifest.json").read_text())
        manifest["counts"]["wiki_versions"] = 4
        (later / "manifest.json").write_text(json.dumps(manifest))

        second = await build_bundle_import(later, continue_import=True).run()

        assert second.created[EntityType.WIKI_VERSION] == 1
        assert second.created[EntityType.WIKI_PAGE] == 0
        contents = (
            (
                await db_session.execute(
                    select(WikiContent).where(WikiContent.page_id == home.id).order_by(WikiContent.version)
                )
            )
            .scalars()
            .all()
        )
        assert [c.version for c in contents] == [1, 2, 3]
        latest = contents[-1]
        assert (latest.text, latest.comments) == ("# Home\n\nThird draft.", "Edited after the first export")
        assert latest.created_at == datetime(2024, 2, 1, 10, tzinfo=UTC)
        assert (await _user(db_session, "bundle_alice")).id == latest.author_id


class TestIssueReferencesAcrossBundles:
    """A source exported in parts: a later bundle's issue placeholders may name issues of an earlier one."""

    def _later(self, root: Path, description: str, *, instance: str = "tracker.example.com") -> Path:
        later = issue("b-1", "p-b", description=description, updated_at="2024-03-02T08:00:00Z")
        return write_bundle(
            root,
            manifest_data=manifest(source={"system": "sample", "instance": instance}),
            projects=[project("p-b", identifier="bundleb")],
            issues=[later],
        )

    async def test_a_placeholder_resolves_to_the_earlier_bundles_issue(self, db_session, build_bundle_import, tmp_path):
        await build_bundle_import(FULL).run()
        later = self._later(tmp_path / "b", "Follows {{issue:web-1}}, see {{issue:web-1|the login bug}}.")

        second = await build_bundle_import(later, continue_import=True).run()

        added = await _issue(db_session, "Issue b-1")
        assert added.description == "Follows BUNDLEWEB-1, see [the login bug](/issue/BUNDLEWEB-1/)."
        # Expanding is not an edit: the bundle's updated_at stays.
        assert added.updated_at == datetime(2024, 3, 2, 8, 0, tzinfo=UTC)
        assert not [w for w in second.warnings if w.message.startswith(PLACEHOLDERS_LEFT)]

    async def test_an_expanded_key_is_not_expanded_again(self, db_session, build_bundle_import, tmp_path):
        """One pass: text that an expansion produces is never read as a placeholder."""
        await build_bundle_import(FULL).run()
        web_1 = await _issue(db_session, "Login fails after the session times out")
        web_1_text = web_1.description
        later = self._later(tmp_path / "b", "{{issue:web-1|{{issue:web-2}}}} and {{issue:web-2}}")

        await build_bundle_import(later, continue_import=True).run()

        added = await _issue(db_session, "Issue b-1")
        assert added.description == "{{issue:web-1|BUNDLEWEB-2}} and BUNDLEWEB-2"
        await db_session.refresh(web_1)
        assert web_1.description == web_1_text

    async def test_a_ref_in_neither_the_bundle_nor_the_map_becomes_its_label_or_ref(
        self, db_session, build_bundle_import, tmp_path
    ):
        await build_bundle_import(FULL).run()
        later = self._later(
            tmp_path / "b", "Follows {{issue:web-1}}, {{issue:gone-9|a [lost] one}} and {{issue:gone-8}}."
        )

        second = await build_bundle_import(later, continue_import=True).run()

        added = await _issue(db_session, "Issue b-1")
        assert added.description == "Follows BUNDLEWEB-1, a [lost] one and gone-8."
        (left,) = [w for w in second.warnings if w.message.startswith(PLACEHOLDERS_LEFT)]
        assert left.context == {"count": 2, "examples": "issue:gone-9, issue:gone-8"}

    async def test_the_same_ref_in_another_scope_does_not_resolve(self, db_session, build_bundle_import, tmp_path):
        """The identifier map is per system and instance: another installation's web-1 is not this one's."""
        await build_bundle_import(FULL).run()
        other = self._later(tmp_path / "b", "Follows {{issue:web-1}}.", instance="other.example.com")

        second = await build_bundle_import(other).run()

        added = await _issue(db_session, "Issue b-1")
        assert added.description == "Follows web-1."
        (left,) = [w for w in second.warnings if w.message.startswith(PLACEHOLDERS_LEFT)]
        assert left.context["examples"] == "issue:web-1"


# How the summary reports texts from earlier imports that this run linked, and those it left alone.
LINKED_NOTE = "Text from earlier imports now linked"
EDITED_TEXT = "Texts edited since an earlier import were left as they are"
LINKS_UNREADABLE = "The links file could not be read"
LINKS_UNWRITABLE = "The links file cannot be written"


def _pending(path: Path) -> LinksStore:
    """The links file at *path*, read-only."""
    return LinksStore.open(path, "sample", "tracker.example.com", writable=False)


class TestLinksFile:
    """Per-project exports in any order: text written as plain text is linked once its records arrive."""

    def _b(
        self,
        root: Path,
        description: str = "Follows {{issue:web-1|the login bug}}.",
        *,
        instance: str = "tracker.example.com",
        journals: list | None = None,
        wiki_pages: list | None = None,
    ) -> Path:
        records: dict = {
            "projects": [project("p-b", identifier="bundleb", modules=["issue_tracking", "wiki"])],
            "issues": [issue("b-1", "p-b", description=description, updated_at="2024-03-02T08:00:00Z")],
        }
        if journals:
            records["journals"] = journals
        if wiki_pages:
            records["wiki_pages"] = wiki_pages
        return write_bundle(root, manifest_data=manifest(source={"system": "sample", "instance": instance}), **records)

    async def test_a_later_import_links_what_an_earlier_one_wrote_as_text(
        self, db_session, build_bundle_import, tmp_path
    ):
        b = self._b(tmp_path / "b")
        await build_bundle_import(b).run()
        later = await _issue(db_session, "Issue b-1")
        assert later.description == "Follows the login bug."
        links = build_bundle_import.links_path(b)
        store = _pending(links)
        assert store.get("issue", later.id, "description").refs == (("issue", "web-1"),)
        store.commit()

        second = await build_bundle_import(FULL, continue_import=True).run()

        await db_session.refresh(later)
        assert later.description == "Follows [the login bug](/issue/BUNDLEWEB-1/)."
        assert later.updated_at == datetime(2024, 3, 2, 8, 0, tzinfo=UTC)
        # The description recorded when the issue was created is linked as well.
        history = (
            await db_session.execute(
                select(JournalDetail.new_value)
                .join(Journal, Journal.id == JournalDetail.journal_id)
                .where(Journal.issue_id == later.id, JournalDetail.prop_key == "description")
            )
        ).scalar_one()
        assert history == later.description
        (linked,) = second.notes[LINKED_NOTE]
        assert linked.startswith("2 text(s), e.g. ")
        assert f"issue {later.id}" in linked
        assert not links.exists()

    async def test_an_edited_text_is_left_alone_and_reported(self, db_session, build_bundle_import, tmp_path):
        b = self._b(tmp_path / "b")
        await build_bundle_import(b).run()
        later = await _issue(db_session, "Issue b-1")
        later.description = "Rewritten by hand."
        await db_session.commit()

        second = await build_bundle_import(FULL, continue_import=True).run()

        await db_session.refresh(later)
        assert later.description == "Rewritten by hand."
        (edited,) = [w for w in second.warnings if w.message.startswith(EDITED_TEXT)]
        assert edited.context == {"count": 1, "examples": f"issue {later.id}"}
        # The edited text is forgotten; the history entry, not edited, was linked, so nothing is left.
        assert not build_bundle_import.links_path(b).exists()

    async def test_only_the_references_that_resolve_are_linked(self, db_session, build_bundle_import, tmp_path):
        b = self._b(tmp_path / "b", "{{issue:web-1}} and {{issue:gone-9}}")
        await build_bundle_import(b).run()

        await build_bundle_import(FULL, continue_import=True).run()

        later = await _issue(db_session, "Issue b-1")
        assert later.description == "BUNDLEWEB-1 and gone-9"
        store = _pending(build_bundle_import.links_path(b))
        waiting = store.get("issue", later.id, "description")
        assert waiting.refs == (("issue", "gone-9"),)
        assert waiting.written_sha256 == text_sha256(later.description)
        assert waiting.source_text == "{{issue:web-1}} and {{issue:gone-9}}"
        store.commit()

    async def test_a_reference_in_another_scope_does_not_resolve(self, db_session, build_bundle_import, tmp_path):
        b = self._b(tmp_path / "b", instance="other.example.com")
        await build_bundle_import(b).run()
        other_links = build_bundle_import.links_path(b, instance="other.example.com")
        before = other_links.read_bytes()

        await build_bundle_import(FULL).run()

        later = await _issue(db_session, "Issue b-1")
        assert later.description == "Follows the login bug."
        assert other_links.read_bytes() == before
        assert not build_bundle_import.links_path(FULL).exists()

    async def test_wiki_versions_and_journal_notes_are_linked_too(self, db_session, build_bundle_import, tmp_path):
        b = self._b(
            tmp_path / "b",
            "No references here.",
            journals=[{"source_ref": "bj-1", "issue_ref": "b-1", "notes": "Duplicate of {{issue:web-1}}."}],
            wiki_pages=[
                {
                    "source_ref": "bw-1",
                    "project_ref": "p-b",
                    "title": "Notes",
                    "versions": [
                        {"source_ref": "bwv-1", "version": 1, "text": "Draft, see {{issue:web-1}}."},
                        {"source_ref": "bwv-2", "version": 2, "text": "Fixed by {{issue:web-1|the login fix}}."},
                    ],
                }
            ],
        )
        await build_bundle_import(b).run()
        note = (await db_session.execute(select(Journal).where(Journal.notes.contains("Duplicate")))).scalar_one()
        assert note.notes == "Duplicate of web-1."
        note_stamp = note.updated_at
        page = await _page(db_session, "Notes")
        versions = (
            (
                await db_session.execute(
                    select(WikiContent).where(WikiContent.page_id == page.id).order_by(WikiContent.version)
                )
            )
            .scalars()
            .all()
        )
        assert [v.text for v in versions] == ["Draft, see web-1.", "Fixed by the login fix."]
        stamps = [v.updated_at for v in versions]

        await build_bundle_import(FULL, continue_import=True).run()

        await db_session.refresh(note)
        assert note.notes == "Duplicate of BUNDLEWEB-1."
        assert note.updated_at == note_stamp
        for version in versions:
            await db_session.refresh(version)
        assert [v.text for v in versions] == [
            "Draft, see BUNDLEWEB-1.",
            "Fixed by [the login fix](/issue/BUNDLEWEB-1/).",
        ]
        assert [v.updated_at for v in versions] == stamps
        assert not build_bundle_import.links_path(b).exists()

    async def test_a_dry_run_reports_what_would_be_linked_and_writes_nothing(
        self, db_session, build_bundle_import, tmp_path
    ):
        b = self._b(tmp_path / "b")
        await build_bundle_import(b).run()
        links = build_bundle_import.links_path(b)
        before = links.read_bytes()

        rehearsal = await build_bundle_import(FULL, continue_import=True, dry_run=True).run()

        assert rehearsal.notes[LINKED_NOTE][0].startswith("2 text(s)")
        await db_session.rollback()
        later = await _issue(db_session, "Issue b-1")
        assert later.description == "Follows the login bug."
        assert links.read_bytes() == before

    async def test_a_corrupt_file_is_reported_and_the_import_goes_on(self, db_session, build_bundle_import, tmp_path):
        b = self._b(tmp_path / "b")
        links = build_bundle_import.links_path(b)
        links.write_bytes(b"not a database " * 100)

        summary = await build_bundle_import(b).run()

        (unreadable,) = [w for w in summary.warnings if w.message.startswith(LINKS_UNREADABLE)]
        assert unreadable.context["path"] == str(links)
        assert (await _issue(db_session, "Issue b-1")).description == "Follows the login bug."
        assert links.read_bytes() == b"not a database " * 100

    async def test_a_file_of_another_scope_is_refused(self, db_session, build_bundle_import, tmp_path):
        foreign = tmp_path / "foreign.sqlite"
        store = LinksStore.open(foreign, "sample", "other.example.com", writable=True)
        store.put(PendingText("issue", 1, "description", "{{issue:x}}", text_sha256("x"), (("issue", "x"),)))
        store.commit()

        with pytest.raises(PhaseFailedError, match=r"belongs to the import of sample @ other\.example\.com"):
            await build_bundle_import(self._b(tmp_path / "b"), links_file=foreign).run()

        await db_session.rollback()
        assert await _count(db_session, Issue) == 0

    @pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permissions")
    async def test_a_read_only_location_is_reported_and_the_import_succeeds(
        self, db_session, build_bundle_import, tmp_path
    ):
        folder = tmp_path / "read-only"
        folder.mkdir()
        folder.chmod(0o555)
        try:
            summary = await build_bundle_import(self._b(tmp_path / "b"), links_file=folder / "links.sqlite").run()
        finally:
            folder.chmod(0o755)

        (unwritable,) = [w for w in summary.warnings if w.message.startswith(LINKS_UNWRITABLE)]
        assert "--links-file" in unwritable.message
        assert (await _issue(db_session, "Issue b-1")).description == "Follows the login bug."
        assert list(folder.iterdir()) == []

    async def test_a_failure_before_the_commit_leaves_the_file_as_it_was(
        self, db_session, build_bundle_import, tmp_path, monkeypatch
    ):
        b = self._b(tmp_path / "b")
        await build_bundle_import(b).run()
        links = build_bundle_import.links_path(b)
        before = links.read_bytes()

        async def broken(ctx):
            raise RuntimeError("link graph failed")

        monkeypatch.setattr("specivo.importers.load.registry.rebuild_wiki_link_graph", broken)
        with pytest.raises(RuntimeError, match="link graph failed"):
            await build_bundle_import(FULL, continue_import=True).run()

        assert links.read_bytes() == before
        assert not links.with_name(links.name + "-journal").exists()

    async def test_a_file_created_meanwhile_is_a_warning_not_a_failure(
        self, db_session, build_bundle_import, tmp_path, monkeypatch
    ):
        """Another process created the file between the existence check and the exclusive create."""
        b = self._b(tmp_path / "b")
        links = build_bundle_import.links_path(b)
        store = LinksStore.open(links, "sample", "tracker.example.com", writable=True)
        store.put(PendingText("issue", 1, "description", "{{issue:x}}", text_sha256("x"), (("issue", "x"),)))
        store.commit()
        before = links.read_bytes()
        monkeypatch.setattr("specivo.importers.core.links_file._refuse_symlink", lambda path: None)

        summary = await build_bundle_import(b).run()

        (refused,) = [w for w in summary.warnings if w.message.startswith(LINKS_UNREADABLE)]
        assert "created by something else" in refused.context["reason"]
        assert (await _issue(db_session, "Issue b-1")).description == "Follows the login bug."
        assert links.read_bytes() == before

    async def test_validate_ignores_the_links_file(self, build_bundle_import, tmp_path):
        b = self._b(tmp_path / "b")
        links = build_bundle_import.links_path(b)
        links.write_bytes(b"garbage")
        args, adapter_cls = parse_args(["--source", "bundle", "--path", str(b), "--validate"], build_source_registry())
        assert await run_validation(args, adapter_cls.from_args(args)) == 0
        assert links.read_bytes() == b"garbage"


# How the summary reports relations and parents across bundles.
LINKS_WAITING = "Relations and parents naming an issue that was not imported"
LINKS_WAITING_NOTE = "Relations and parents waiting for issues a later import may bring"
RELATION_REFUSED = "Relation was rejected and skipped"
OTHER_PARENT = "Issue already has a different parent; the one the source names was not set"


def _part(root: Path, project_ref: str, identifier: str, issues: list, relations: list | None = None) -> Path:
    """One project of the source, as a bundle of its own."""
    records: dict = {"projects": [project(project_ref, identifier=identifier)], "issues": issues}
    if relations:
        records["relations"] = relations
    return write_bundle(root, **records)


def _rel(source_ref: str, from_ref: str, relation_type: str, to_ref: str) -> dict:
    return {"source_ref": source_ref, "from_ref": from_ref, "to_ref": to_ref, "relation_type": relation_type}


async def _relations(session) -> list[tuple[str, str, str]]:
    """Every relation as ``(from subject, type, to subject)``, a ``relates`` one with its ends sorted."""
    subjects = dict((await session.execute(select(Issue.id, Issue.subject))).all())
    found = []
    for relation in (await session.execute(select(IssueRelation))).scalars():
        ends = (subjects[relation.issue_from_id], subjects[relation.issue_to_id])
        if relation.relation_type == "relates":
            ends = tuple(sorted(ends))
        found.append((ends[0], relation.relation_type, ends[1]))
    return sorted(found)


async def _parent_subject(session, subject: str) -> str | None:
    child = await _issue(session, subject)
    await session.refresh(child)
    if child.parent_id is None:
        return None
    return (await session.get(Issue, child.parent_id)).subject


class TestRelationsAndParentsAcrossBundles:
    """A source exported one project at a time: links between the projects arrive whichever part comes first.

    Part A carries a-1 and a-2 and two relations to part B's issues; part B carries b-1 and b-2, whose
    parent is a-2, and one of A's two relations, as a tool writes a link between projects into both.
    """

    def _a(self, root: Path, **changes) -> Path:
        relations = changes.pop(
            "relations", [_rel("r-x", "a-1", "blocks", "b-1"), _rel("r-a", "a-1", "relates", "b-2")]
        )
        return _part(root, "p-a", "parta", [issue("a-1", "p-a"), issue("a-2", "p-a")], relations)

    def _b(self, root: Path, **changes) -> Path:
        issues = changes.pop("issues", [issue("b-1", "p-b"), issue("b-2", "p-b", parent_ref="a-2")])
        relations = changes.pop("relations", [_rel("r-x", "a-1", "blocks", "b-1")])
        return _part(root, "p-b", "partb", issues, relations)

    EXPECTED = [("Issue a-1", "blocks", "Issue b-1"), ("Issue a-1", "relates", "Issue b-2")]

    async def _assert_linked(self, session, build_bundle_import, *bundles: Path) -> None:
        assert await _relations(session) == self.EXPECTED
        assert await _parent_subject(session, "Issue b-2") == "Issue a-2"
        for bundle in bundles:
            assert not build_bundle_import.links_path(bundle).exists()

    async def test_a_then_b(self, db_session, build_bundle_import, tmp_path):
        a, b = self._a(tmp_path / "a"), self._b(tmp_path / "b")

        first = await build_bundle_import(a).run()

        assert await _relations(db_session) == []
        store = _pending(build_bundle_import.links_path(a))
        assert [(r.source_ref, r.from_ref, r.relation_type, r.to_ref) for r in store.relations()] == [
            ("r-a", "a-1", "relates", "b-2"),
            ("r-x", "a-1", "blocks", "b-1"),
        ]
        assert store.parents() == []
        store.rollback()
        (waiting,) = [w for w in first.warnings if w.message.startswith(LINKS_WAITING)]
        assert (waiting.context["relations"], waiting.context["parents"]) == (2, 0)
        assert first.notes[LINKS_WAITING_NOTE] == [f"2 relation(s), kept in {build_bundle_import.links_path(a)}"]

        second = await build_bundle_import(b, continue_import=True).run()

        await self._assert_linked(db_session, build_bundle_import, a)
        assert second.created[EntityType.RELATION] == 2
        assert not [w for w in second.warnings if w.message.startswith(LINKS_WAITING)]

    async def test_b_then_a(self, db_session, build_bundle_import, tmp_path):
        a, b = self._a(tmp_path / "a"), self._b(tmp_path / "b")

        first = await build_bundle_import(b).run()

        assert await _parent_subject(db_session, "Issue b-2") is None
        store = _pending(build_bundle_import.links_path(b))
        assert [(p.child_ref, p.parent_ref) for p in store.parents()] == [("b-2", "a-2")]
        assert [r.source_ref for r in store.relations()] == ["r-x"]
        store.rollback()
        (waiting,) = [w for w in first.warnings if w.message.startswith(LINKS_WAITING)]
        assert (waiting.context["relations"], waiting.context["parents"]) == (1, 1)

        await build_bundle_import(a, continue_import=True).run()

        await self._assert_linked(db_session, build_bundle_import, b)

    async def test_a_relation_carried_by_both_parts_is_made_once(self, db_session, build_bundle_import, tmp_path):
        a, b = self._a(tmp_path / "a"), self._b(tmp_path / "b")
        await build_bundle_import(a).run()
        await build_bundle_import(b, continue_import=True).run()

        blocks = [r for r in await _relations(db_session) if r[1] == "blocks"]
        assert blocks == [("Issue a-1", "blocks", "Issue b-1")]
        mapped = (
            await db_session.execute(
                select(ImportIdMapping.source_id).where(ImportIdMapping.entity_type == str(EntityType.RELATION))
            )
        ).scalars()
        assert sorted(mapped) == ["r-a", "r-x"]

    @pytest.mark.parametrize("order", ["ab", "ba"])
    async def test_loading_the_parts_again_creates_nothing(self, db_session, build_bundle_import, tmp_path, order):
        parts = {"a": self._a(tmp_path / "a"), "b": self._b(tmp_path / "b")}
        for name in order:
            await build_bundle_import(parts[name], continue_import=name != order[0]).run()
        before = await _counts(db_session)

        for name in order + order:
            summary = await build_bundle_import(parts[name], continue_import=True).run()
            assert sum(summary.created.values()) == 0
            assert not [w for w in summary.warnings if w.message.startswith(LINKS_WAITING)]

        assert await _counts(db_session) == before
        await self._assert_linked(db_session, build_bundle_import, *parts.values())

    async def test_a_parent_in_a_later_project_of_the_same_bundle(self, db_session, build_bundle_import, tmp_path):
        bundle = write_bundle(
            tmp_path / "one",
            projects=[project("p1", identifier="first"), project("p2", identifier="second")],
            issues=[issue("i1", "p1", parent_ref="i2"), issue("i2", "p2")],
            relations=[_rel("r1", "i1", "relates", "i2")],
        )

        summary = await build_bundle_import(bundle).run()

        assert await _parent_subject(db_session, "Issue i1") == "Issue i2"
        # The relation is refused once the hierarchy is in: Specivo relates no issue to its parent.
        (refused,) = [w for w in summary.warnings if w.message.startswith(RELATION_REFUSED)]
        assert refused.context["relation"] == "r1"
        assert not build_bundle_import.links_path(bundle).exists()

    async def test_a_relation_to_the_issues_own_descendant_is_reported_not_made(
        self, db_session, build_bundle_import, tmp_path
    ):
        """A's relation waits for b-1; B makes b-1 a child of a-1, so the relation is refused, with a warning."""
        a = self._a(tmp_path / "a", relations=[_rel("r-d", "a-1", "relates", "b-1")])
        b = self._b(tmp_path / "b", issues=[issue("b-1", "p-b", parent_ref="a-1")], relations=[])
        await build_bundle_import(a).run()

        second = await build_bundle_import(b, continue_import=True).run()

        assert await _parent_subject(db_session, "Issue b-1") == "Issue a-1"
        assert await _relations(db_session) == []
        (refused,) = [w for w in second.warnings if w.message.startswith(RELATION_REFUSED)]
        assert refused.context["relation"] == "r-d"
        assert "descendant" in refused.context["reason"]
        assert not build_bundle_import.links_path(a).exists()

    async def test_a_parent_given_by_hand_meanwhile_is_kept(self, db_session, build_bundle_import, tmp_path):
        from specivo.services.nested_set_service import NestedSetService

        b = self._b(tmp_path / "b", issues=[issue("b-1", "p-b"), issue("b-2", "p-b", parent_ref="a-2")], relations=[])
        await build_bundle_import(b).run()
        b_1, b_2 = await _issue(db_session, "Issue b-1"), await _issue(db_session, "Issue b-2")
        await NestedSetService().move_to_parent(db_session, b_2, b_1)
        await db_session.commit()

        second = await build_bundle_import(self._a(tmp_path / "a", relations=[]), continue_import=True).run()

        assert await _parent_subject(db_session, "Issue b-2") == "Issue b-1"
        (kept,) = [w for w in second.warnings if w.message.startswith(OTHER_PARENT)]
        assert kept.context["issue"] == "b-2"
        assert not build_bundle_import.links_path(b).exists()

    async def test_a_parent_from_an_earlier_part_keeps_both_issues_timestamps(
        self, db_session, build_bundle_import, tmp_path
    ):
        a = _part(tmp_path / "a", "p-a", "parta", [issue("a-2", "p-a", updated_at="2024-01-05T10:00:00Z")])
        b = self._b(
            tmp_path / "b",
            issues=[issue("b-2", "p-b", parent_ref="a-2", updated_at="2024-02-05T10:00:00Z")],
            relations=[],
        )
        await build_bundle_import(a).run()

        await build_bundle_import(b, continue_import=True).run()

        assert await _parent_subject(db_session, "Issue b-2") == "Issue a-2"
        assert (await _issue(db_session, "Issue a-2")).updated_at == datetime(2024, 1, 5, 10, tzinfo=UTC)
        assert (await _issue(db_session, "Issue b-2")).updated_at == datetime(2024, 2, 5, 10, tzinfo=UTC)

    async def test_a_tampered_relation_row_is_refused(self, db_session, build_bundle_import, tmp_path):
        a = self._a(tmp_path / "a")
        await build_bundle_import(a).run()
        links = build_bundle_import.links_path(a)
        conn = sqlite3.connect(links)
        conn.execute(
            "UPDATE pending_relation SET to_ref = 'b-1', relation_type = 'duplicates' WHERE source_ref = 'r-a'"
        )
        conn.commit()
        conn.close()

        summary = await build_bundle_import(self._b(tmp_path / "b"), continue_import=True).run()

        assert await _relations(db_session) == [("Issue a-1", "blocks", "Issue b-1")]
        (untrusted,) = [w for w in summary.warnings if w.message.startswith(LINKS_UNTRUSTED)]
        assert untrusted.context["count"] == 1
        assert not links.exists()

    async def test_a_dry_run_reports_and_writes_nothing(self, db_session, build_bundle_import, tmp_path):
        a = self._a(tmp_path / "a")
        await build_bundle_import(a).run()
        links = build_bundle_import.links_path(a)
        before = links.read_bytes()

        summary = await build_bundle_import(self._b(tmp_path / "b"), continue_import=True, dry_run=True).run()

        assert summary.created[EntityType.RELATION] == 2
        await db_session.rollback()
        assert await _relations(db_session) == []
        assert links.read_bytes() == before


class TestPendingLinksCommands:
    """``admin import-map pending`` and ``resolve-pending`` cover waiting relations and parents."""

    async def _waiting(self, db_session, build_bundle_import, tmp_path) -> Path:
        """A loaded first; B loaded with a links file of its own, so A's file still waits."""
        parts = TestRelationsAndParentsAcrossBundles()
        a = parts._a(tmp_path / "a")
        await build_bundle_import(a).run()
        await build_bundle_import(
            parts._b(tmp_path / "b"), continue_import=True, links_file=tmp_path / "b.sqlite"
        ).run()
        return build_bundle_import.links_path(a)

    async def test_pending_counts_them(self, db_session, build_bundle_import, tmp_path, capsys):
        from argparse import Namespace

        from specivo.cli.admin import _run_import_map_pending

        links = await self._waiting(db_session, build_bundle_import, tmp_path)

        assert await _run_import_map_pending(Namespace(links_file=str(links))) == 0

        assert capsys.readouterr().out.splitlines() == [
            "sample @ tracker.example.com: 2 relation(s) wait for issues not imported yet",
        ]

    async def test_a_dry_run_of_resolve_pending_writes_nothing(self, db_session, build_bundle_import, tmp_path):
        links = await self._waiting(db_session, build_bundle_import, tmp_path)
        before = links.read_bytes()

        outcome = await resolve_links_file(db_session, links, dry_run=True)

        # r-x was made by B; r-a can be made now.
        assert (outcome.relations_linked, outcome.parents_linked, outcome.links_waiting) == (1, 0, 0)
        assert await _relations(db_session) == [("Issue a-1", "blocks", "Issue b-1")]
        assert links.read_bytes() == before

    async def test_resolve_pending_makes_them_and_removes_the_file(self, db_session, build_bundle_import, tmp_path):
        links = await self._waiting(db_session, build_bundle_import, tmp_path)

        outcome = await resolve_links_file(db_session, links, dry_run=False)

        assert (outcome.relations_linked, outcome.links_waiting) == (1, 0)
        assert await _relations(db_session) == TestRelationsAndParentsAcrossBundles.EXPECTED
        assert not links.exists()
        again = await resolve_links_file(db_session, links, dry_run=False)
        assert again.scope is None


class TestResolvePendingCommand:
    """``admin import-map resolve-pending``: the resolve step on demand, against a links file."""

    async def _waiting(self, db_session, build_bundle_import, tmp_path) -> Path:
        """B loaded first, then A loaded with a links file of its own, so B's file still waits."""
        b = TestLinksFile()._b(tmp_path / "b")
        await build_bundle_import(b).run()
        await build_bundle_import(FULL, continue_import=True, links_file=tmp_path / "a-links.sqlite").run()
        return build_bundle_import.links_path(b)

    async def test_nothing_waiting_is_a_no_op(self, db_session, tmp_path):
        outcome = await resolve_links_file(db_session, tmp_path / "missing.sqlite", dry_run=False)
        assert outcome.scope is None
        assert (outcome.linked, outcome.edited, outcome.waiting) == ([], [], 0)
        assert not (tmp_path / "missing.sqlite").exists()

    async def test_a_dry_run_reports_without_writing(self, db_session, build_bundle_import, tmp_path):
        links = await self._waiting(db_session, build_bundle_import, tmp_path)
        before = links.read_bytes()
        later = await _issue(db_session, "Issue b-1")

        outcome = await resolve_links_file(db_session, links, dry_run=True)

        assert outcome.scope == ("sample", "tracker.example.com")
        assert len(outcome.linked) == 2
        assert outcome.waiting == 0
        await db_session.refresh(later)
        assert later.description == "Follows the login bug."
        assert links.read_bytes() == before

    async def test_with_yes_it_links_and_removes_the_emptied_file(self, db_session, build_bundle_import, tmp_path):
        links = await self._waiting(db_session, build_bundle_import, tmp_path)

        outcome = await resolve_links_file(db_session, links, dry_run=False)

        later = await _issue(db_session, "Issue b-1")
        assert later.description == "Follows [the login bug](/issue/BUNDLEWEB-1/)."
        assert later.updated_at == datetime(2024, 3, 2, 8, 0, tzinfo=UTC)
        assert ("issue", later.id) in outcome.linked
        assert not links.exists()


LINKS_UNTRUSTED = "Rows of the links file that this installation did not write were ignored"
LINKS_FOREIGN = "Rows of the links file naming records this source did not import were ignored"


class TestForgedLinksFile:
    """Whoever supplies the bundle folder can supply a links file too: it must not reach other rows."""

    async def _setup(self, db_session, build_bundle_import, tmp_path):
        """B imported (its file waits for web-1), a victim imported by another source, web-1 now mapped."""
        b = TestLinksFile()._b(tmp_path / "b")
        await build_bundle_import(b).run()
        victim_bundle = write_bundle(
            tmp_path / "v",
            manifest_data=manifest(source={"system": "sample", "instance": "victim.example.com"}),
            projects=[project("p-v", identifier="victim")],
            issues=[issue("v-1", "p-v", description="Original text."), issue("v-2", "p-v")],
        )
        await build_bundle_import(victim_bundle).run()
        victim = await _issue(db_session, "Issue v-1")
        # web-1 arrives in this scope; any issue but the victim will do as its target.
        target = await _issue(db_session, "Issue v-2")
        await ImportIdMap("sample", "tracker.example.com", uuid.uuid4()).put(
            db_session, EntityType.ISSUE, "web-1", "issues", target.id
        )
        await db_session.commit()
        return build_bundle_import.links_path(b), victim

    def _forge(
        self,
        path: Path,
        row_id: int,
        current: str,
        *,
        key: bytes | None,
        column: str = "description",
        entity: str = "issue",
    ) -> None:
        forged = PendingText(
            entity, row_id, column, "{{issue:web-1}} was here", text_sha256(current), (("issue", "web-1"),)
        )
        if key is None:
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO pending (entity_type, row_id, column_name, source_text, written_sha256, signature) "
                "VALUES (?, ?, ?, ?, ?, '')",
                (entity, row_id, column, forged.source_text, forged.written_sha256),
            )
            conn.execute("INSERT INTO pending_ref VALUES (?, ?, ?, 'issue', 'web-1')", (entity, row_id, column))
            conn.commit()
            conn.close()
            return
        store = LinksStore.open(path, "sample", "tracker.example.com", writable=True, key=key)
        store.put(forged)
        store.commit()

    @pytest.mark.parametrize("key", [None, links_signing_key("another-installations-secret-key-0123456789")])
    async def test_a_forged_row_without_this_installations_signature_is_not_written(
        self, db_session, build_bundle_import, tmp_path, key
    ):
        links, victim = await self._setup(db_session, build_bundle_import, tmp_path)
        self._forge(links, victim.id, victim.description, key=key)

        outcome = await resolve_links_file(db_session, links, dry_run=False)

        await db_session.refresh(victim)
        assert victim.description == "Original text."
        assert outcome.untrusted == 1
        assert ("issue", victim.id) not in outcome.linked
        if links.exists():
            assert _pending(links).get("issue", victim.id, "description") is None

    async def test_a_signed_row_naming_a_record_this_source_did_not_import_is_not_written(
        self, db_session, build_bundle_import, tmp_path
    ):
        """Even a genuine signature (a file this installation wrote, edited) cannot reach outside the scope."""
        links, victim = await self._setup(db_session, build_bundle_import, tmp_path)
        self._forge(links, victim.id, victim.description, key=links_signing_key())

        outcome = await resolve_links_file(db_session, links, dry_run=False)

        await db_session.refresh(victim)
        assert victim.description == "Original text."
        assert outcome.foreign == 1

    async def test_a_signed_row_for_an_imported_issue_is_linked(self, db_session, build_bundle_import, tmp_path):
        links, victim = await self._setup(db_session, build_bundle_import, tmp_path)
        later = await _issue(db_session, "Issue b-1")

        outcome = await resolve_links_file(db_session, links, dry_run=False)

        await db_session.refresh(later)
        assert later.description == f"Follows [the login bug](/issue/{victim.project_key}-2/)."
        assert ("issue", later.id) in outcome.linked
        assert (outcome.untrusted, outcome.foreign) == (0, 0)

    @pytest.mark.parametrize(("entity", "column"), [("issue", "subject"), ("project", "description")])
    async def test_an_unknown_column_or_entity_is_dropped(
        self, db_session, build_bundle_import, tmp_path, entity, column
    ):
        links, victim = await self._setup(db_session, build_bundle_import, tmp_path)
        self._forge(links, victim.id, victim.subject, key=links_signing_key(), entity=entity, column=column)

        outcome = await resolve_links_file(db_session, links, dry_run=False)

        await db_session.refresh(victim)
        assert victim.subject == "Issue v-1"
        assert outcome.untrusted == 1
        if links.exists():
            assert _pending(links).get(entity, victim.id, column) is None

    async def test_the_import_reports_forged_rows(self, db_session, build_bundle_import, tmp_path):
        links, victim = await self._setup(db_session, build_bundle_import, tmp_path)
        self._forge(links, victim.id, victim.description, key=None)

        summary = await build_bundle_import(FULL, continue_import=True).run()

        await db_session.refresh(victim)
        assert victim.description == "Original text."
        (untrusted,) = [w for w in summary.warnings if w.message.startswith(LINKS_UNTRUSTED)]
        assert untrusted.context["count"] == 1

    async def test_a_symlinked_links_file_is_refused_with_a_warning(self, db_session, build_bundle_import, tmp_path):
        b = TestLinksFile()._b(tmp_path / "b")
        real = tmp_path / "real.sqlite"
        LinksStore.open(real, "sample", "tracker.example.com", writable=True).commit()
        links = build_bundle_import.links_path(b)
        links.symlink_to(tmp_path / "elsewhere.sqlite")

        summary = await build_bundle_import(b).run()

        (refused,) = [w for w in summary.warnings if w.message.startswith(LINKS_UNREADABLE)]
        assert "symbolic link" in refused.context["reason"]
        assert not (tmp_path / "elsewhere.sqlite").exists()
        assert (await _issue(db_session, "Issue b-1")).description == "Follows the login bug."


def _data_only_zip(source: Path, target: Path) -> Path:
    """Zip the bundle at *source* without its ``files/``, as a tool does when the bytes go elsewhere."""
    with zipfile.ZipFile(target, "w") as archive:
        for path in sorted(source.iterdir()):
            if path.is_file():
                archive.write(path, path.name)
    return target


class TestSeparateFiles:
    """``--files-path``: records from a zip, bytes from a directory copied over on their own."""

    async def test_a_data_only_zip_loads_every_attachment_from_the_directory(
        self, db_session, build_bundle_import, tmp_path
    ):
        archive = _data_only_zip(FULL, tmp_path / "data.zip")

        summary = await build_bundle_import(archive, files_path=FULL / "files").run()

        assert summary.created[EntityType.ATTACHMENT] == 3
        attachments = {a.filename: a for a in (await db_session.execute(select(Attachment))).scalars()}
        assert attachments["deploy.log"].content_hash == SHA_LOG
        assert attachments["notes.txt"].content_hash == SHA_NOTES
        for attachment in attachments.values():
            stored_copy = build_bundle_import.storage / attachment.disk_filename
            assert hashlib.sha256(stored_copy.read_bytes()).hexdigest() == attachment.content_hash

    async def test_a_dry_run_writes_nothing(self, db_session, build_bundle_import, tmp_path):
        archive = _data_only_zip(FULL, tmp_path / "data.zip")

        summary = await build_bundle_import(archive, files_path=FULL / "files", dry_run=True).run()

        assert summary.created[EntityType.ATTACHMENT] == 3
        await db_session.rollback()
        assert await _count(db_session, Attachment) == 0
        assert list(build_bundle_import.storage.iterdir()) == []

    async def test_a_rerun_after_new_files_arrive_creates_only_the_new_attachments(
        self, db_session, build_bundle_import, tmp_path
    ):
        files = tmp_path / "store"
        shutil.copytree(FULL / "files", files)
        await build_bundle_import(_data_only_zip(FULL, tmp_path / "first.zip"), files_path=files).run()

        later = copy_full(tmp_path / "later")
        body = b"Release checklist\n"
        (files / stored(body)).write_bytes(body)
        record = {
            "source_ref": "a-checklist",
            "container_kind": "issue",
            "container_ref": "web-2",
            "filename": "checklist.txt",
            "sha256": stored(body),
            "filesize": len(body),
        }
        with (later / "attachments.jsonl").open("a") as handle:
            handle.write(json.dumps(record) + "\n")
        manifest = json.loads((later / "manifest.json").read_text())
        manifest["counts"]["attachments"] += 1
        (later / "manifest.json").write_text(json.dumps(manifest))

        second = await build_bundle_import(
            _data_only_zip(later, tmp_path / "second.zip"), files_path=files, continue_import=True
        ).run()

        assert second.created[EntityType.ATTACHMENT] == 1
        assert second.skipped[EntityType.ATTACHMENT] == 3
        added = (
            await db_session.execute(select(Attachment).where(Attachment.filename == "checklist.txt"))
        ).scalar_one()
        assert (build_bundle_import.storage / added.disk_filename).read_bytes() == body

    async def test_a_bad_directory_refuses_the_import_before_anything_is_written(
        self, db_session, build_bundle_import, tmp_path
    ):
        files = tmp_path / "store"
        shutil.copytree(FULL / "files", files)
        (files / SHA_LOG).write_bytes(b"tampered")

        with pytest.raises(BundleInvalidError, match=r"attachments\.jsonl:1: error: files/bb18"):
            await build_bundle_import(_data_only_zip(FULL, tmp_path / "data.zip"), files_path=files).run()

        assert await _count(db_session, ImportIdMapping) == 0


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
