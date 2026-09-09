"""Service tests for the user loader.

Specivo is stricter about identity than Redmine: an address is required and
both login and email are unique case-insensitively. These tests pin what
happens when the source does not meet that, and that no credential is ever
carried across.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from specivo.importers.core.ir import EntityType, IRGroup, IRUser
from specivo.importers.load.user_loader import (
    NOTE_PASSWORD_RESET,
    NOTE_SYNTHETIC_EMAIL,
    ensure_import_account,
    load_groups,
    load_users,
)
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.services.auth_utils import verify_password
from tests.services.conftest import FakeAdapter

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]


def _user(**overrides) -> IRUser:
    data = {
        "source_ref": "12",
        "login": "alex",
        "display_name": "Alex Kim",
        "email": "alex@example.org",
        "status": "active",
        "is_admin": False,
        "language": "fr",
        "last_login_at": datetime(2026, 3, 1, 8, 0, tzinfo=UTC),
        "created_at": datetime(2019, 1, 5, 9, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 2, 2, 10, 0, tzinfo=UTC),
    }
    data.update(overrides)
    return IRUser(**data)


class TestLoadUser:
    async def test_creates_the_account(self, db_session, make_context):
        ctx = make_context(FakeAdapter(users=[_user()]))
        await load_users(ctx)

        user_id = await ctx.id_map.get(db_session, EntityType.USER, "12")
        user = await db_session.get(User, user_id)
        assert user.login == "alex"
        assert user.email == "alex@example.org"
        assert user.display_name == "Alex Kim"
        assert user.status == "active"
        assert user.is_service_account is False

    async def test_original_timestamps_are_restored(self, db_session, make_context):
        """An account created in 2019 should not say it was created today."""
        ctx = make_context(FakeAdapter(users=[_user()]))
        await load_users(ctx)

        user_id = await ctx.id_map.get(db_session, EntityType.USER, "12")
        await db_session.refresh(await db_session.get(User, user_id))
        user = await db_session.get(User, user_id)
        assert user.created_at == datetime(2019, 1, 5, 9, 0, tzinfo=UTC)
        assert user.updated_at == datetime(2026, 2, 2, 10, 0, tzinfo=UTC)

    async def test_last_login_is_kept(self, db_session, make_context):
        ctx = make_context(FakeAdapter(users=[_user()]))
        await load_users(ctx)
        user_id = await ctx.id_map.get(db_session, EntityType.USER, "12")
        user = await db_session.get(User, user_id)
        assert user.last_login_at == datetime(2026, 3, 1, 8, 0, tzinfo=UTC)

    async def test_admin_flag_carries_over(self, db_session, make_context):
        ctx = make_context(FakeAdapter(users=[_user(is_admin=True)]))
        await load_users(ctx)
        user_id = await ctx.id_map.get(db_session, EntityType.USER, "12")
        assert (await db_session.get(User, user_id)).is_admin is True

    async def test_locked_account_stays_locked(self, db_session, make_context):
        ctx = make_context(FakeAdapter(users=[_user(status="locked")]))
        await load_users(ctx)
        user_id = await ctx.id_map.get(db_session, EntityType.USER, "12")
        assert (await db_session.get(User, user_id)).status == "locked"


class TestCredentials:
    async def test_password_is_unusable(self, db_session, make_context):
        """Redmine hashes with SHA1 and Specivo with bcrypt; nothing transfers."""
        ctx = make_context(FakeAdapter(users=[_user()]))
        await load_users(ctx)

        user_id = await ctx.id_map.get(db_session, EntityType.USER, "12")
        user = await db_session.get(User, user_id)
        assert user.password_hash is not None
        assert not verify_password("alex", user.password_hash)
        assert not verify_password("", user.password_hash)

    async def test_two_accounts_do_not_share_a_hash(self, db_session, make_context):
        users = [_user(), _user(source_ref="13", login="sam", email="sam@example.org")]
        ctx = make_context(FakeAdapter(users=users))
        await load_users(ctx)

        stmt = select(User.password_hash).where(User.login.in_(["alex", "sam"]))
        hashes = (await db_session.execute(stmt)).scalars().all()
        assert len(set(hashes)) == 2

    async def test_every_login_is_listed_for_reset(self, db_session, make_context):
        """The accounts are unreachable until somebody resets them."""
        ctx = make_context(FakeAdapter(users=[_user()]))
        await load_users(ctx)
        assert ctx.summary.notes[NOTE_PASSWORD_RESET] == ["alex"]


class TestIdentityAdjustments:
    async def test_missing_email_gets_a_placeholder(self, db_session, make_context):
        """Specivo requires an address; .invalid can never resolve."""
        ctx = make_context(FakeAdapter(users=[_user(email=None)]))
        await load_users(ctx)

        user_id = await ctx.id_map.get(db_session, EntityType.USER, "12")
        user = await db_session.get(User, user_id)
        assert user.email == "alex@invalid"
        assert ctx.summary.notes[NOTE_SYNTHETIC_EMAIL] == ["alex (alex@invalid)"]

    async def test_taken_login_is_suffixed(self, db_session, make_context):
        db_session.add(User(login="alex", email="other@example.org", display_name="Other", status="active"))
        await db_session.flush()

        ctx = make_context(FakeAdapter(users=[_user()]))
        await load_users(ctx)

        user_id = await ctx.id_map.get(db_session, EntityType.USER, "12")
        assert (await db_session.get(User, user_id)).login == "alex-2"
        assert any("Login already taken" in w.message for w in ctx.summary.warnings)

    async def test_login_collision_ignores_case(self, db_session, make_context):
        """Uniqueness is on LOWER(login), so ALEX and alex collide."""
        db_session.add(User(login="ALEX", email="other@example.org", display_name="Other", status="active"))
        await db_session.flush()

        ctx = make_context(FakeAdapter(users=[_user()]))
        await load_users(ctx)
        user_id = await ctx.id_map.get(db_session, EntityType.USER, "12")
        assert (await db_session.get(User, user_id)).login == "alex-2"

    async def test_taken_email_is_suffixed(self, db_session, make_context):
        db_session.add(User(login="other", email="alex@example.org", display_name="Other", status="active"))
        await db_session.flush()

        ctx = make_context(FakeAdapter(users=[_user()]))
        await load_users(ctx)

        user_id = await ctx.id_map.get(db_session, EntityType.USER, "12")
        assert (await db_session.get(User, user_id)).email == "alex+2@example.org"

    async def test_empty_login_is_generated_from_the_source_id(self, db_session, make_context):
        ctx = make_context(FakeAdapter(users=[_user(login="")]))
        await load_users(ctx)

        user_id = await ctx.id_map.get(db_session, EntityType.USER, "12")
        assert (await db_session.get(User, user_id)).login == "user-12"

    async def test_overlong_login_is_truncated_to_the_column(self, db_session, make_context):
        ctx = make_context(FakeAdapter(users=[_user(login="a" * 200)]))
        await load_users(ctx)

        user_id = await ctx.id_map.get(db_session, EntityType.USER, "12")
        assert len((await db_session.get(User, user_id)).login) == 100

    async def test_unshipped_language_falls_back(self, db_session, make_context):
        """A locale this build has no catalog for would render as raw keys.

        Portuguese is the realistic case: Redmine ships pt-BR, which normalises
        to pt, and Specivo has no pt catalog.
        """
        ctx = make_context(FakeAdapter(users=[_user(language="pt")]))
        await load_users(ctx)

        user_id = await ctx.id_map.get(db_session, EntityType.USER, "12")
        assert (await db_session.get(User, user_id)).language == "en"

    async def test_shipped_language_is_kept(self, db_session, make_context):
        ctx = make_context(FakeAdapter(users=[_user(language="fr")]))
        await load_users(ctx)

        user_id = await ctx.id_map.get(db_session, EntityType.USER, "12")
        assert (await db_session.get(User, user_id)).language == "fr"


class TestImportAccount:
    async def test_creates_a_service_account(self, db_session, make_context):
        ctx = make_context(FakeAdapter())
        account = await ensure_import_account(ctx)

        assert account.login == "redmine-import"
        assert account.is_service_account is True
        assert account.is_admin is False

    async def test_is_reused_on_a_second_call(self, db_session, make_context):
        ctx = make_context(FakeAdapter())
        first = await ensure_import_account(ctx)
        second = await ensure_import_account(ctx)
        assert first.id == second.id

        count = (
            await db_session.execute(select(func.count()).select_from(User).where(User.login == "redmine-import"))
        ).scalar_one()
        assert count == 1

    async def test_a_resumed_run_finds_the_existing_account(self, db_session, make_context):
        """A fresh run id must not create a second import account."""
        first = await ensure_import_account(make_context(FakeAdapter()))
        second = await ensure_import_account(make_context(FakeAdapter()))
        assert first.id == second.id


class TestGroups:
    async def test_the_group_is_created_with_its_members(self, db_session, make_context):
        users = [
            _user(source_ref="1", login="grp_alex", email="grp_alex@example.org"),
            _user(source_ref="2", login="grp_sam", email="grp_sam@example.org"),
        ]
        groups = [IRGroup(source_ref="20", name="Platform Team", member_refs=["1", "2"])]
        ctx = make_context(FakeAdapter(users=users, groups=groups))
        await load_users(ctx)
        await load_groups(ctx)

        group_id = await ctx.id_map.get(db_session, EntityType.GROUP, "20")
        group = await db_session.get(UserGroup, group_id)
        assert group.name == "Platform Team"

        member_ids = set(
            (
                await db_session.execute(select(UserGroupMember.user_id).where(UserGroupMember.group_id == group_id))
            ).scalars()
        )
        assert member_ids == {
            await ctx.id_map.get(db_session, EntityType.USER, "1"),
            await ctx.id_map.get(db_session, EntityType.USER, "2"),
        }

    async def test_created_groups_are_counted(self, db_session, make_context):
        """The report says groups came across; it no longer warns about them."""
        groups = [IRGroup(source_ref="20", name="Counted Team", member_refs=[])]
        ctx = make_context(FakeAdapter(groups=groups))
        await load_groups(ctx)

        assert ctx.summary.created[EntityType.GROUP] == 1
        assert ctx.summary.warnings == []

    async def test_a_member_that_was_not_imported_is_reported_not_fatal(self, db_session, make_context):
        users = [_user(source_ref="1", login="grp_kept", email="grp_kept@example.org")]
        groups = [IRGroup(source_ref="20", name="Partial Team", member_refs=["1", "999"])]
        ctx = make_context(FakeAdapter(users=users, groups=groups))
        await load_users(ctx)
        await load_groups(ctx)

        group_id = await ctx.id_map.get(db_session, EntityType.GROUP, "20")
        count = (
            await db_session.execute(
                select(func.count()).select_from(UserGroupMember).where(UserGroupMember.group_id == group_id)
            )
        ).scalar_one()
        assert count == 1
        assert any("left out of the group" in w.message for w in ctx.summary.warnings)

    async def test_two_source_groups_differing_only_in_case_stay_separate(self, db_session, make_context):
        """user_groups is unique on the lowercased name and Redmine is not."""
        groups = [
            IRGroup(source_ref="20", name="Case Team", member_refs=[]),
            IRGroup(source_ref="21", name="case team", member_refs=[]),
        ]
        ctx = make_context(FakeAdapter(groups=groups))
        await load_groups(ctx)

        first = await db_session.get(UserGroup, await ctx.id_map.get(db_session, EntityType.GROUP, "20"))
        second = await db_session.get(UserGroup, await ctx.id_map.get(db_session, EntityType.GROUP, "21"))
        assert first.id != second.id
        assert second.name == "case team-2"
        assert any("already taken" in w.message for w in ctx.summary.warnings)

    async def test_a_name_an_existing_group_holds_is_suffixed_not_reused(self, db_session, make_context):
        """Merging into it would hand its members access nobody granted them."""
        existing = UserGroup(name="Existing Team")
        db_session.add(existing)
        await db_session.flush()

        ctx = make_context(FakeAdapter(groups=[IRGroup(source_ref="20", name="Existing Team", member_refs=[])]))
        await load_groups(ctx)

        imported_id = await ctx.id_map.get(db_session, EntityType.GROUP, "20")
        assert imported_id != existing.id
        assert (await db_session.get(UserGroup, imported_id)).name == "Existing Team-2"

    async def test_a_resumed_run_finds_the_group_it_already_made(self, db_session, make_context):
        groups = [IRGroup(source_ref="20", name="Resumed Team", member_refs=[])]
        first = make_context(FakeAdapter(groups=groups))
        await load_groups(first)
        group_id = await first.id_map.get(db_session, EntityType.GROUP, "20")

        second = make_context(FakeAdapter(groups=groups))
        await load_groups(second)

        assert await second.id_map.get(db_session, EntityType.GROUP, "20") == group_id
        assert second.summary.created[EntityType.GROUP] == 0
        assert second.summary.skipped[EntityType.GROUP] == 1
        count = (
            await db_session.execute(
                select(func.count()).select_from(UserGroup).where(UserGroup.name.like("Resumed Team%"))
            )
        ).scalar_one()
        assert count == 1

    async def test_no_groups_means_no_warning(self, db_session, make_context):
        ctx = make_context(FakeAdapter(groups=[]))
        await load_groups(ctx)
        assert ctx.summary.warnings == []
        assert ctx.summary.created[EntityType.GROUP] == 0


class TestIdempotency:
    async def test_second_run_creates_no_duplicate(self, db_session, make_context):
        ctx = make_context(FakeAdapter(users=[_user()]))
        await load_users(ctx)

        again = make_context(FakeAdapter(users=[_user()]))
        await load_users(again)

        count = (
            await db_session.execute(select(func.count()).select_from(User).where(User.login == "alex"))
        ).scalar_one()
        assert count == 1
        assert again.summary.skipped[EntityType.USER] == 1
        assert again.summary.created[EntityType.USER] == 0
