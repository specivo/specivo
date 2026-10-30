"""Notifications about an issue reach only the people who can see it.

A private project whose members hold ``view_issues`` with
``issues_visibility = 'all'``, and in it one private issue that everyone below
watches:

- ``author`` wrote it and ``assignee`` is assigned to it: private issues show
  to them, so they are notified;
- ``member`` holds the same role but is neither: they cannot see the private
  issue and get nothing, though they stay a watcher;
- ``outsider`` has no membership, ``admin`` none either but sees everything;
- ``locked`` is a member whose account is locked and gets nothing;
- ``actor`` makes every change and never hears about their own action.

Every check covers the in-app notification and the queued email.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.member import Member, MemberRole
from specivo.models.notification import Notification
from specivo.models.project import Project
from specivo.models.role import Role, RoleBuiltin
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.models.watcher import Watcher
from specivo.services.anonymous_access_service import set_anonymous_access_enabled, set_anonymous_permissions
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from specivo.services.mention_service import MentionService
from specivo.services.notification_service import NotificationService
from specivo.services.permission_service import clear_role_cache
from specivo.services.watcher_service import WatcherService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_notifications = NotificationService()
_issues = IssueService()
_watchers = WatcherService()

WATCHERS = ("author", "assignee", "member", "outsider", "admin", "locked", "actor")


async def _user(db: AsyncSession, login: str, *, admin: bool = False, status: str = "active") -> User:
    factory = AdminUserFactory if admin else UserFactory
    user: User = factory.build(login=login, status=status, email=f"{login}@example.com")
    db.add(user)
    await db.flush()
    return user


async def _member(db: AsyncSession, project: Project, user: User, role: Role) -> Member:
    member = Member(project_id=project.id, user_id=user.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()
    return member


class World:
    def __init__(self, project: Project, role: Role, users: dict[str, User], lookups: dict[str, int]) -> None:
        self.project = project
        self.role = role
        self.users = users
        self.lookups = lookups

    async def issue(self, db: AsyncSession, *, private: bool, watchers: tuple[str, ...] = WATCHERS) -> Issue:
        issue: Issue = IssueFactory.build(
            project_id=self.project.id,
            project_key=self.project.key,
            subject="Nrv secret subject" if private else "Nrv public subject",
            is_private=private,
            author_id=self.users["author"].id,
            assigned_to_id=self.users["assignee"].id,
            **self.lookups,
        )
        db.add(issue)
        await db.flush()
        for name in watchers:
            db.add(Watcher(issue_id=issue.id, user_id=self.users[name].id))
        await db.flush()
        return issue


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    status = StatusFactory.build(name="Nrv Open")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Nrv Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Nrv Normal")
    project = ProjectFactory.build(key="NRV", identifier="nrv", is_public=False)
    role = Role(name="Nrv Developer", permissions=["view_issues", "edit_issues"], builtin=0, issues_visibility="all")
    db_session.add_all([tracker, priority, project, role])
    await db_session.flush()

    users = {
        "author": await _user(db_session, "nrv_author"),
        "assignee": await _user(db_session, "nrv_assignee"),
        "member": await _user(db_session, "nrv_member"),
        "outsider": await _user(db_session, "nrv_outsider"),
        "admin": await _user(db_session, "nrv_admin", admin=True),
        "locked": await _user(db_session, "nrv_locked", status="locked"),
        "actor": await _user(db_session, "nrv_actor"),
    }
    for name in ("author", "assignee", "member", "locked", "actor"):
        await _member(db_session, project, users[name], role)
    lookups = {"tracker_id": tracker.id, "status_id": status.id, "priority_id": priority.id}
    return World(project, role, users, lookups)


@contextmanager
def _emails() -> Iterator[MagicMock]:
    with patch("specivo.tasks.notifications.send_notification_email.delay") as delay:
        yield delay


def _emailed(delay: MagicMock) -> set[str]:
    return {call.args[0] for call in delay.call_args_list}


async def _notified(db: AsyncSession, issue: Issue) -> set[int]:
    rows = await db.execute(
        select(Notification.user_id).where(Notification.entity_type == "issue", Notification.entity_id == issue.id)
    )
    return set(rows.scalars().all())


def _ids(world: World, *names: str) -> set[int]:
    return {world.users[name].id for name in names}


def _addresses(world: World, *names: str) -> set[str]:
    return {world.users[name].email for name in names}


async def _comment(db: AsyncSession, issue: Issue, actor: User, notes: str = "Nrv a comment") -> Journal:
    return await JournalService().add_comment(session=db, issue=issue, user=actor, notes=notes)


# ---------------------------------------------------------------------------
# Private issue: update, comment and assignment events
# ---------------------------------------------------------------------------


async def test_update_on_private_issue_skips_watchers_who_cannot_see_it(db_session: AsyncSession, world: World):
    issue = await world.issue(db_session, private=True)

    with _emails() as delay:
        await _notifications.notify_watchers(db_session, issue, "status_change", world.users["actor"])

    assert await _notified(db_session, issue) == _ids(world, "author", "assignee", "admin")
    assert _emailed(delay) == _addresses(world, "author", "assignee", "admin")


async def test_comment_on_private_issue_skips_watchers_who_cannot_see_it(db_session: AsyncSession, world: World):
    issue = await world.issue(db_session, private=True)
    journal = await _comment(db_session, issue, world.users["actor"])

    with _emails() as delay:
        await _notifications.notify_comment(db_session, issue, journal, world.users["actor"])

    assert await _notified(db_session, issue) == _ids(world, "author", "assignee", "admin")
    assert _emailed(delay) == _addresses(world, "author", "assignee", "admin")


async def test_assignment_to_user_who_cannot_see_the_issue_is_not_notified(db_session: AsyncSession, world: World):
    issue = await world.issue(db_session, private=True, watchers=())
    issue.assigned_to_id = world.users["outsider"].id
    await db_session.flush()

    with _emails() as delay:
        await _notifications.notify_assignment(
            db_session, issue, world.users["assignee"].id, world.users["outsider"].id, world.users["actor"]
        )

    assert await _notified(db_session, issue) == set()
    assert _emailed(delay) == set()


async def test_assignment_to_member_who_can_see_the_issue_is_notified(db_session: AsyncSession, world: World):
    issue = await world.issue(db_session, private=True, watchers=())
    issue.assigned_to_id = world.users["member"].id
    await db_session.flush()

    with _emails() as delay:
        await _notifications.notify_assignment(
            db_session, issue, world.users["assignee"].id, world.users["member"].id, world.users["actor"]
        )

    assert await _notified(db_session, issue) == _ids(world, "member")
    assert _emailed(delay) == _addresses(world, "member")


async def test_assignee_who_lost_membership_gets_no_assignment_or_comment(db_session: AsyncSession, world: World):
    issue = await world.issue(db_session, private=False, watchers=())
    await db_session.execute(
        delete(Member).where(Member.project_id == world.project.id, Member.user_id == world.users["assignee"].id)
    )
    clear_role_cache(db_session)
    journal = await _comment(db_session, issue, world.users["actor"])

    with _emails() as delay:
        await _notifications.notify_assignment(
            db_session, issue, None, world.users["assignee"].id, world.users["actor"]
        )
        await _notifications.notify_comment(db_session, issue, journal, world.users["actor"])

    assert await _notified(db_session, issue) == set()
    assert _emailed(delay) == set()


# ---------------------------------------------------------------------------
# Losing and regaining access
# ---------------------------------------------------------------------------


async def test_watcher_who_lost_membership_is_skipped_and_notified_again_once_back(
    db_session: AsyncSession, world: World
):
    issue = await world.issue(db_session, private=False, watchers=("member",))
    await db_session.execute(
        delete(Member).where(Member.project_id == world.project.id, Member.user_id == world.users["member"].id)
    )
    clear_role_cache(db_session)

    with _emails() as delay:
        await _notifications.notify_watchers(db_session, issue, "status_change", world.users["actor"])
    assert await _notified(db_session, issue) == set()
    assert _emailed(delay) == set()
    # Still a watcher, so access coming back restores the notifications.
    assert [u.id for u in await _watchers.list_watchers(db_session, issue)] == [world.users["member"].id]

    await _member(db_session, world.project, world.users["member"], world.role)
    clear_role_cache(db_session)
    with _emails() as delay:
        await _notifications.notify_watchers(db_session, issue, "status_change", world.users["actor"])
    assert await _notified(db_session, issue) == _ids(world, "member")
    assert _emailed(delay) == _addresses(world, "member")


async def test_watchers_without_access_stay_in_the_watcher_list(db_session: AsyncSession, world: World):
    issue = await world.issue(db_session, private=True)

    await _notifications.notify_watchers(db_session, issue, "status_change", world.users["actor"])

    listed = {u.id for u in await _watchers.list_watchers(db_session, issue)}
    assert listed == _ids(world, *WATCHERS)


# ---------------------------------------------------------------------------
# Who keeps being notified
# ---------------------------------------------------------------------------


async def test_public_issue_notifies_every_member_and_admin_but_not_outsider_locked_or_actor(
    db_session: AsyncSession, world: World
):
    issue = await world.issue(db_session, private=False)

    with _emails() as delay:
        await _notifications.notify_watchers(db_session, issue, "status_change", world.users["actor"])

    expected = ("author", "assignee", "member", "admin")
    assert await _notified(db_session, issue) == _ids(world, *expected)
    assert _emailed(delay) == _addresses(world, *expected)


async def test_locked_admin_gets_nothing(db_session: AsyncSession, world: World):
    issue = await world.issue(db_session, private=False, watchers=("admin",))
    world.users["admin"].status = "locked"
    await db_session.flush()

    with _emails() as delay:
        await _notifications.notify_watchers(db_session, issue, "status_change", world.users["actor"])

    assert await _notified(db_session, issue) == set()
    assert _emailed(delay) == set()


async def test_deactivated_member_gets_nothing(db_session: AsyncSession, world: World):
    issue = await world.issue(db_session, private=False, watchers=("member",))
    world.users["member"].status = "deactivated"
    await db_session.flush()

    await _notifications.notify_watchers(db_session, issue, "status_change", world.users["actor"])

    assert await _notified(db_session, issue) == set()


async def test_role_without_view_issues_gets_nothing(db_session: AsyncSession, world: World):
    issue = await world.issue(db_session, private=False, watchers=("member",))
    blind = Role(name="Nrv Blind", permissions=["edit_issues"], builtin=0, issues_visibility="all")
    db_session.add(blind)
    await db_session.flush()
    await db_session.execute(
        update(MemberRole)
        .where(
            MemberRole.member_id.in_(
                select(Member.id).where(
                    Member.project_id == world.project.id, Member.user_id == world.users["member"].id
                )
            )
        )
        .values(role_id=blind.id)
    )

    await _notifications.notify_watchers(db_session, issue, "status_change", world.users["actor"])

    assert await _notified(db_session, issue) == set()


async def test_own_visibility_notifies_only_author_and_assignee(db_session: AsyncSession, world: World):
    world.role.issues_visibility = "own"
    await db_session.flush()
    issue = await world.issue(db_session, private=False, watchers=("author", "assignee", "member"))

    await _notifications.notify_watchers(db_session, issue, "status_change", world.users["actor"])

    assert await _notified(db_session, issue) == _ids(world, "author", "assignee")


async def test_group_membership_counts(db_session: AsyncSession, world: World):
    issue = await world.issue(db_session, private=False, watchers=("outsider",))
    group = UserGroup(name="nrv-group")
    db_session.add(group)
    await db_session.flush()
    db_session.add(UserGroupMember(group_id=group.id, user_id=world.users["outsider"].id))
    member = Member(project_id=world.project.id, group_id=group.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=world.role.id))
    await db_session.flush()

    await _notifications.notify_watchers(db_session, issue, "status_change", world.users["actor"])

    assert await _notified(db_session, issue) == _ids(world, "outsider")


async def test_public_project_non_member_follows_the_non_member_role(db_session: AsyncSession, world: World):
    world.project.is_public = True
    await db_session.flush()
    issue = await world.issue(db_session, private=False, watchers=("outsider",))
    non_member = Role.builtin == RoleBuiltin.NON_MEMBER

    await db_session.execute(update(Role).where(non_member).values(permissions=[], issues_visibility="all"))
    clear_role_cache(db_session)
    await _notifications.notify_watchers(db_session, issue, "status_change", world.users["actor"])
    assert await _notified(db_session, issue) == set()

    await db_session.execute(update(Role).where(non_member).values(permissions=["view_issues"]))
    clear_role_cache(db_session)
    await _notifications.notify_watchers(db_session, issue, "status_change", world.users["actor"])
    assert await _notified(db_session, issue) == _ids(world, "outsider")


async def test_public_project_non_member_follows_anonymous_access(db_session: AsyncSession, world: World):
    world.project.is_public = True
    await db_session.flush()
    issue = await world.issue(db_session, private=False, watchers=("outsider",))
    await db_session.execute(update(Role).where(Role.builtin == RoleBuiltin.NON_MEMBER).values(permissions=[]))
    clear_role_cache(db_session)

    await _notifications.notify_watchers(db_session, issue, "status_change", world.users["actor"])
    assert await _notified(db_session, issue) == set()

    await set_anonymous_permissions(db_session, world.project, ["view_issues"], world.users["admin"])
    await set_anonymous_access_enabled(db_session, True, world.users["admin"], confirmed_projects=["NRV"])
    clear_role_cache(db_session)

    assert await _issues.is_visible(db_session, issue, world.users["outsider"])
    await _notifications.notify_watchers(db_session, issue, "status_change", world.users["actor"])
    assert await _notified(db_session, issue) == _ids(world, "outsider")


async def test_membership_without_view_issues_is_not_topped_up_by_the_non_member_role(
    db_session: AsyncSession, world: World
):
    world.project.is_public = True
    await db_session.flush()
    issue = await world.issue(db_session, private=False, watchers=("member", "outsider"))
    await db_session.execute(
        update(Role)
        .where(Role.builtin == RoleBuiltin.NON_MEMBER)
        .values(permissions=["view_issues"], issues_visibility="all")
    )
    blind = Role(name="Nrv Blind Public", permissions=["edit_issues"], builtin=0, issues_visibility="all")
    db_session.add(blind)
    await db_session.flush()
    await db_session.execute(
        update(MemberRole)
        .where(
            MemberRole.member_id.in_(
                select(Member.id).where(
                    Member.project_id == world.project.id, Member.user_id == world.users["member"].id
                )
            )
        )
        .values(role_id=blind.id)
    )
    clear_role_cache(db_session)

    assert not await _issues.is_visible(db_session, issue, world.users["member"])
    await _notifications.notify_watchers(db_session, issue, "status_change", world.users["actor"])

    assert await _notified(db_session, issue) == _ids(world, "outsider")


async def test_actor_is_never_notified_even_as_admin_watcher(db_session: AsyncSession, world: World):
    issue = await world.issue(db_session, private=False, watchers=("admin",))

    await _notifications.notify_watchers(db_session, issue, "status_change", world.users["admin"])

    assert await _notified(db_session, issue) == set()


# ---------------------------------------------------------------------------
# The batch check
# ---------------------------------------------------------------------------


async def test_batch_check_agrees_with_is_visible(db_session: AsyncSession, world: World):
    for private in (False, True):
        issue = await world.issue(db_session, private=private)
        ids = [u.id for u in world.users.values()]

        allowed = {u.id for u in await _issues.users_who_can_view(db_session, issue, ids)}

        expected = {
            u.id
            for u in world.users.values()
            if u.status == "active" and await _issues.is_visible(db_session, issue, u)
        }
        assert allowed == expected, private


async def test_batch_check_uses_a_constant_number_of_queries(db_session: AsyncSession, world: World):
    issue = await world.issue(db_session, private=False, watchers=())
    extra = [await _user(db_session, f"nrv_extra_{n}") for n in range(20)]
    for user in extra:
        await _member(db_session, world.project, user, world.role)

    async def count(user_ids: list[int]) -> tuple[int, int]:
        statements: list[str] = []

        def _count(conn, cursor, statement, *args) -> None:  # noqa: ANN001
            statements.append(statement)

        clear_role_cache(db_session)
        engine = db_session.bind.sync_engine  # type: ignore[union-attr]
        event.listen(engine, "before_cursor_execute", _count)
        try:
            allowed = await _issues.users_who_can_view(db_session, issue, user_ids)
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        return len(allowed), len(statements)

    few, few_queries = await count([u.id for u in extra[:2]])
    many, many_queries = await count([u.id for u in extra])

    assert (few, many) == (2, 20)
    assert many_queries == few_queries


async def test_one_visibility_check_per_event(db_session: AsyncSession, world: World):
    issue = await world.issue(db_session, private=False, watchers=())
    extra = [await _user(db_session, f"nrv_extra_{n}") for n in range(20)]
    for user in extra:
        await _member(db_session, world.project, user, world.role)
        db_session.add(Watcher(issue_id=issue.id, user_id=user.id))
    await db_session.flush()
    journal = await _comment(db_session, issue, world.users["actor"])

    original = IssueService.users_who_can_view
    calls: list[list[int]] = []

    async def spy(self: IssueService, session: AsyncSession, issue: Issue, user_ids: list[int]) -> list[User]:
        calls.append(list(user_ids))
        return await original(self, session, issue, user_ids)

    with patch.object(IssueService, "users_who_can_view", spy):
        await _notifications.notify_comment(db_session, issue, journal, world.users["actor"])

    assert len(calls) == 1
    assert await _notified(db_session, issue) == {u.id for u in extra} | _ids(world, "assignee")


# ---------------------------------------------------------------------------
# Mentions
# ---------------------------------------------------------------------------


async def test_mention_of_user_who_cannot_see_the_issue_is_not_notified(db_session: AsyncSession, world: World):
    issue = await world.issue(db_session, private=True, watchers=())
    text = "Nrv ping @nrv_member @nrv_author @nrv_outsider @nrv_admin"
    journal = await _comment(db_session, issue, world.users["actor"], notes=text)

    await MentionService().process_mentions(db_session, journal, text, world.users["actor"])

    assert await _notified(db_session, issue) == _ids(world, "author", "admin")
