"""End-to-end import of the seeded Redmine fixture.

Unit and service tests check the importer against IR we wrote ourselves. These
check it against Redmine: its schema, its own bookkeeping, its storage layout
and the shapes its models actually produce.

Requires the fixture:

    make redmine-fixture-up && make redmine-fixture-seed

Marked ``serial``: an import writes instance-wide rows — roles, statuses,
activities, accounts — that other tests insert too. Run in parallel with the
rest of the suite, two workers end up waiting on each other's uncommitted index
entries and the run deadlocks. ``make test-serial`` runs these.

Both profiles are exercised, since the importer supports both databases Redmine
runs on and the schemas are only identical in theory.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from specivo.importers.core.ir import EntityType
from specivo.models.attachment import Attachment
from specivo.models.import_id_map import ImportIdMapping
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.relation import IssueRelation
from specivo.models.role import Role
from specivo.models.time_entry import TimeEntry
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.models.version import Version
from specivo.models.wiki import WikiContent, WikiPage, WikiRedirect
from specivo.services.permission_service import clear_role_cache, get_user_roles

pytestmark = [
    pytest.mark.asyncio(loop_scope="function"),
    pytest.mark.integration,
    pytest.mark.redmine,
    pytest.mark.serial,
    pytest.mark.slow,
]


async def _count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


class TestDryRun:
    async def test_nothing_is_written(self, db_session, build_import):
        """The report is trustworthy because the real writes ran and rolled back."""
        summary = await build_import(dry_run=True, merge_duplicate_statuses=True).run()

        assert summary.created[EntityType.ISSUE] > 0
        await db_session.rollback()

        for model in (Project, Issue, WikiPage, Attachment, ImportIdMapping):
            assert await _count(db_session, model) == 0, model.__name__

    async def test_no_files_are_copied(self, db_session, build_import):
        """The database rolls back; a copied file would not."""
        builder = build_import(dry_run=True, merge_duplicate_statuses=True)
        await builder.run()
        assert list(build_import.storage.iterdir()) == []


class TestFullImport:
    @pytest.fixture
    async def imported(self, db_session, build_import):
        summary = await build_import(merge_duplicate_statuses=True).run()
        return summary

    async def test_the_run_reports_no_failures(self, db_session, imported):
        """The only warning the seeded fixture produces is the importer
        explaining itself: hours rounded to two decimal places."""
        messages = [warning.message for warning in imported.warnings]
        unexpected = [m for m in messages if "rounded" not in m]
        assert unexpected == []

    async def test_projects_including_the_subproject(self, db_session, imported):
        projects = {p.identifier: p for p in (await db_session.execute(select(Project))).scalars()}
        assert {"acme-app", "acme-mobile", "acme-legacy"} <= set(projects)
        assert projects["acme-mobile"].parent_id == projects["acme-app"].id

    async def test_an_archived_project_stays_archived(self, db_session, imported):
        archived = (await db_session.execute(select(Project).where(Project.identifier == "acme-legacy"))).scalar_one()
        assert archived.status == 9

    async def test_every_issue_arrives(self, db_session, imported):
        """Including the one on a locked version, which cannot be created
        directly and is why versions are imported open and locked afterwards."""
        subjects = set((await db_session.execute(select(Issue.subject))).scalars())
        assert "Login fails after session timeout" in subjects
        assert "Session cookie is not cleared" in subjects
        assert "ปัญหาการเข้าสู่ระบบ" in subjects

    async def test_the_locked_version_ends_up_locked(self, db_session, imported):
        version = (await db_session.execute(select(Version).where(Version.name == "1.0"))).scalar_one()
        assert version.status == "locked"
        assert version.sharing == "descendants"

    async def test_the_subtask_keeps_its_parent(self, db_session, imported):
        parent = (
            await db_session.execute(select(Issue).where(Issue.subject == "Login fails after session timeout"))
        ).scalar_one()
        child = (
            await db_session.execute(select(Issue).where(Issue.subject == "Session cookie is not cleared"))
        ).scalar_one()
        assert child.parent_id == parent.id
        assert parent.lft < child.lft < child.rgt < parent.rgt

    async def test_the_private_issue_stays_private(self, db_session, imported):
        issue = (await db_session.execute(select(Issue).where(Issue.subject == "Crash on cold start"))).scalar_one()
        assert issue.is_private is True

    async def test_textile_became_markdown(self, db_session, imported):
        issue = (
            await db_session.execute(select(Issue).where(Issue.subject == "Login fails after session timeout"))
        ).scalar_one()
        assert issue.description.startswith("## Steps to reproduce")
        assert "```ruby" in issue.description
        assert "[[Architecture]]" in issue.description

    async def test_the_issue_reference_became_a_display_key(self, db_session, imported):
        issue = (
            await db_session.execute(select(Issue).where(Issue.subject == "Login fails after session timeout"))
        ).scalar_one()
        assert "Related to #2" not in issue.description
        assert "Related to ACMEAPP-" in issue.description

    async def test_custom_fields_became_metadata(self, db_session, imported):
        issue = (
            await db_session.execute(select(Issue).where(Issue.subject == "Login fails after session timeout"))
        ).scalar_one()
        metadata = issue.issue_metadata
        assert metadata["severity"] == "High"
        assert metadata["tags"] == ["ui", "backend"]
        assert metadata["story_points"] == 5
        assert metadata["effort"] == 2.25
        assert metadata["regression"] is True
        assert metadata["reference"] == "REF-9001"

    async def test_user_and_version_references_resolve_to_specivo_ids(self, db_session, imported):
        """They are stored as source ids first, since the referenced row may be
        imported after the issue that names it."""
        issue = (
            await db_session.execute(select(Issue).where(Issue.subject == "Login fails after session timeout"))
        ).scalar_one()
        reviewer = await db_session.get(User, issue.issue_metadata["reviewer"])
        version = await db_session.get(Version, issue.issue_metadata["target_release"])
        assert reviewer is not None
        assert version is not None and version.name == "1.0"

    async def test_history_is_replayed_in_order(self, db_session, imported):
        issue = (
            await db_session.execute(select(Issue).where(Issue.subject == "Login fails after session timeout"))
        ).scalar_one()
        journals = (
            (await db_session.execute(select(Journal).where(Journal.issue_id == issue.id).order_by(Journal.sequence)))
            .scalars()
            .all()
        )
        sequences = [journal.sequence for journal in journals]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)
        assert any(journal.notes and "Reproduced on staging" in journal.notes for journal in journals)

    async def test_a_private_note_stays_private(self, db_session, imported):
        count = (
            await db_session.execute(select(func.count()).select_from(Journal).where(Journal.is_private.is_(True)))
        ).scalar_one()
        assert count == 1

    async def test_relations_including_one_across_projects(self, db_session, imported):
        relations = (await db_session.execute(select(IssueRelation))).scalars().all()
        types = {relation.relation_type for relation in relations}
        assert {"relates", "blocks", "precedes"} <= types
        assert any(relation.delay == 2 for relation in relations)

    async def test_wiki_history_is_complete(self, db_session, imported):
        page = (await db_session.execute(select(WikiPage).where(WikiPage.title == "Architecture"))).scalar_one()
        versions = (
            (
                await db_session.execute(
                    select(WikiContent.version).where(WikiContent.page_id == page.id).order_by(WikiContent.version)
                )
            )
            .scalars()
            .all()
        )
        assert versions == [1, 2, 3]

    async def test_the_wiki_rename_left_a_working_redirect(self, db_session, imported):
        redirect = (
            await db_session.execute(select(WikiRedirect).where(WikiRedirect.title_from == "old-runbook"))
        ).scalar_one()
        assert redirect.redirected_to == "runbook"

    async def test_attachments_arrive_with_their_bytes(self, db_session, imported, build_import):
        attachments = {a.filename: a for a in (await db_session.execute(select(Attachment))).scalars()}
        assert "server.log" in attachments
        assert "diagram.png" in attachments

        stored = build_import.storage / attachments["server.log"].disk_filename
        assert stored.exists()
        assert stored.stat().st_size == attachments["server.log"].filesize

    async def test_a_file_type_no_longer_accepted_still_arrives(self, db_session, imported):
        """The allowlist governs uploads today; history predates it."""
        filenames = set((await db_session.execute(select(Attachment.filename))).scalars())
        assert "legacy-tool.exe" in filenames

    async def test_a_wiki_attachment_is_on_its_page(self, db_session, imported):
        attachment = (
            await db_session.execute(select(Attachment).where(Attachment.filename == "notes.txt"))
        ).scalar_one()
        assert attachment.container_type == "WikiPage"
        page = await db_session.get(WikiPage, attachment.container_id)
        assert page is not None and page.title == "Architecture"

    async def test_the_group_came_across_with_its_members(self, db_session, imported):
        group = (await db_session.execute(select(UserGroup).where(UserGroup.name == "Platform Team"))).scalar_one()
        logins = set(
            (
                await db_session.execute(
                    select(User.login)
                    .join(UserGroupMember, UserGroupMember.user_id == User.id)
                    .where(UserGroupMember.group_id == group.id)
                )
            ).scalars()
        )
        assert logins == {"fixture_dev", "fixture_thai"}

    async def test_the_group_holds_its_roles_on_the_project(self, db_session, imported):
        """One membership row held by the group, not a copy per member."""
        project = (await db_session.execute(select(Project).where(Project.identifier == "acme-app"))).scalar_one()
        group = (await db_session.execute(select(UserGroup).where(UserGroup.name == "Platform Team"))).scalar_one()

        member = (
            await db_session.execute(select(Member).where(Member.project_id == project.id, Member.group_id == group.id))
        ).scalar_one()
        role_names = set(
            (
                await db_session.execute(
                    select(Role.name)
                    .join(MemberRole, MemberRole.role_id == Role.id)
                    .where(MemberRole.member_id == member.id)
                )
            ).scalars()
        )
        assert role_names == {"Developer"}

    async def test_the_groups_members_resolve_to_its_roles(self, db_session, imported):
        """The access the group's grant is supposed to hand its members.

        Neither account holds a membership of its own on this project, so
        Developer can only be reaching them through the group.
        """
        project = (await db_session.execute(select(Project).where(Project.identifier == "acme-app"))).scalar_one()
        clear_role_cache()

        for login in ("fixture_dev", "fixture_thai"):
            user = (await db_session.execute(select(User).where(User.login == login))).scalar_one()
            direct = (
                await db_session.execute(
                    select(Member).where(Member.project_id == project.id, Member.user_id == user.id)
                )
            ).scalar_one_or_none()
            assert direct is None, login

            roles = await get_user_roles(db_session, user.id, project.id)
            assert [role.name for role in roles] == ["Developer"], login

    async def test_logged_time_is_rounded_to_two_places(self, db_session, imported):
        entries = (await db_session.execute(select(TimeEntry))).scalars().all()
        assert len(entries) == 3
        assert all(entry.hours == entry.hours.quantize(Decimal("0.01")) for entry in entries)

    async def test_imported_accounts_cannot_sign_in_with_a_guessable_password(self, db_session, imported):
        """No credential is portable, so every account gets an unusable hash."""
        from specivo.services.auth_utils import verify_password

        user = (await db_session.execute(select(User).where(User.login == "fixture_dev"))).scalar_one()
        assert user.password_hash is not None
        assert not verify_password("fixture-password-not-a-secret", user.password_hash)

    async def test_the_report_lists_the_accounts_owing_a_password(self, db_session, imported):
        note = imported.notes["accounts_that_will_be_asked_to_set_a_password_at_first_sign_in"]
        assert "fixture_dev" in note

    async def test_imported_people_must_set_their_own_password(self, db_session, imported):
        user = (await db_session.execute(select(User).where(User.login == "fixture_dev"))).scalar_one()
        assert user.must_change_password is True

    async def test_the_import_service_account_is_not_flagged(self, db_session, imported):
        """It has no password to replace; the CHECK on users would reject the row."""
        account = (await db_session.execute(select(User).where(User.login == "redmine-import"))).scalar_one()
        assert account.is_service_account is True
        assert account.must_change_password is False


class TestRerun:
    async def test_a_second_import_creates_nothing(self, db_session, build_import):
        await build_import(merge_duplicate_statuses=True).run()
        before = {
            model.__name__: await _count(db_session, model)
            for model in (Project, Issue, Journal, WikiPage, Attachment, TimeEntry, IssueRelation)
        }

        second = await build_import(merge_duplicate_statuses=True).run()

        after = {
            model.__name__: await _count(db_session, model)
            for model in (Project, Issue, Journal, WikiPage, Attachment, TimeEntry, IssueRelation)
        }
        assert after == before
        assert second.created[EntityType.ISSUE] == 0
        assert second.skipped[EntityType.ISSUE] > 0

    async def test_a_second_import_copies_no_more_files(self, db_session, build_import):
        await build_import(merge_duplicate_statuses=True).run()
        first = sorted(path.name for path in build_import.storage.iterdir())

        await build_import(merge_duplicate_statuses=True).run()
        assert sorted(path.name for path in build_import.storage.iterdir()) == first
