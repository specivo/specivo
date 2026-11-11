"""Description versions and restore only reach the journals of the issue in the URL.

``/issue/{ref}/versions/{journal_id}/`` and its ``restore/`` POST take a
journal id from the URL. Access is checked for the issue named there, so a
journal of any other issue (another project, a private issue) must be a 404,
and a private journal is hidden from non-admins as in the activity feed.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

SECRET = "Secret roadmap of the other project."
HIDDEN = "Text only admins may read."


@dataclass
class World:
    admin: User
    member: User
    issue: Issue
    own_journal: Journal
    private_journal: Journal
    foreign_journal: Journal


async def _journal(db: AsyncSession, issue: Issue, user: User, seq: int, old: str, new: str, **kw: object) -> Journal:
    journal = Journal(issue_id=issue.id, project_id=issue.project_id, user_id=user.id, sequence=seq, **kw)
    db.add(journal)
    await db.flush()
    db.add(JournalDetail(journal_id=journal.id, property="attr", prop_key="description", old_value=old, new_value=new))
    await db.flush()
    return journal


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    db = db_session
    status = StatusFactory.build(name="DVS New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="DVS Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="DVS Normal", position=1)
    db.add_all([tracker, priority])
    await db.flush()

    mine = ProjectFactory.build(key="DVS", identifier="dvs-mine", is_public=False)
    other = ProjectFactory.build(key="DVO", identifier="dvs-other", is_public=False)
    db.add_all([mine, other])
    await db.flush()
    db.add_all([EnabledModule(project_id=p.id, name="issue_tracking") for p in (mine, other)])

    admin = AdminUserFactory.build(login="dvs_admin", status="active")
    member = UserFactory.build(login="dvs_member", status="active")
    db.add_all([admin, member])
    await db.flush()

    role = Role(
        name="DVS Developer",
        permissions=["view_issues", "add_issues", "edit_issues", "add_notes"],
        builtin=0,
        issues_visibility="default",
    )
    db.add(role)
    await db.flush()
    membership = Member(user_id=member.id, project_id=mine.id)
    db.add(membership)
    await db.flush()
    db.add(MemberRole(member_id=membership.id, role_id=role.id))

    def issue(project: Project, description: str) -> Issue:
        return IssueFactory.build(
            project_id=project.id,
            project_key=project.key,
            sequence_number=1,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
            author_id=admin.id,
            subject=f"{project.key} issue",
            description=description,
        )

    my_issue = issue(mine, "Current text.")
    their_issue = issue(other, SECRET)
    db.add_all([my_issue, their_issue])
    await db.flush()

    own = await _journal(db, my_issue, member, 1, "First text.", "Current text.")
    private = await _journal(db, my_issue, admin, 2, HIDDEN, "Current text.", is_private=True)
    foreign = await _journal(db, their_issue, admin, 1, "Older secret.", SECRET)
    await db.commit()
    return World(admin, member, my_issue, own, private, foreign)


def _cookies(user: User, **extra: str) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings()), **extra}


async def _diff(client: AsyncClient, user: User, journal: Journal) -> tuple[int, str]:
    resp = await client.get(f"/issue/DVS-1/versions/{journal.id}/", cookies=_cookies(user))
    return resp.status_code, resp.text


async def _csrf(client: AsyncClient, user: User) -> str:
    """A signed CSRF token, as the middleware sets it on any GET."""
    resp = await client.get("/issue/DVS-1/", cookies=_cookies(user))
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith("csrf_token="):
            return value.split("=", 1)[1].split(";")[0].strip()
    raise AssertionError("no csrf_token cookie set")


async def _restore(client: AsyncClient, user: User, journal: Journal) -> int:
    csrf = await _csrf(client, user)
    resp = await client.post(
        f"/issue/DVS-1/versions/{journal.id}/restore/",
        data={"csrf_token": csrf},
        cookies=_cookies(user, csrf_token=csrf),
        headers={"x-csrf-token": csrf},
        follow_redirects=False,
    )
    return resp.status_code


async def test_member_sees_the_versions_of_the_issue(client: AsyncClient, world: World) -> None:
    status, html = await _diff(client, world.member, world.own_journal)
    assert status == 200, html[:500]
    assert "First text." in html


async def test_journal_of_another_issue_is_not_found(client: AsyncClient, world: World) -> None:
    status, html = await _diff(client, world.member, world.foreign_journal)
    assert status == 404
    assert SECRET not in html and "Older secret." not in html


async def test_restore_cannot_copy_another_issues_description(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    assert await _restore(client, world.member, world.foreign_journal) == 404
    await db_session.refresh(world.issue)
    assert world.issue.description == "Current text."


async def test_restore_of_own_version_still_works(client: AsyncClient, db_session: AsyncSession, world: World) -> None:
    assert await _restore(client, world.member, world.own_journal) == 302
    await db_session.refresh(world.issue)
    assert world.issue.description == "First text."


async def test_private_journal_is_hidden_from_non_admins(client: AsyncClient, world: World) -> None:
    status, html = await _diff(client, world.member, world.private_journal)
    assert status == 404 and HIDDEN not in html
    assert await _restore(client, world.member, world.private_journal) == 404
    # Nor is it listed among the versions of a change the member may see.
    _status, html = await _diff(client, world.member, world.own_journal)
    assert HIDDEN not in html and f"/versions/{world.private_journal.id}/" not in html

    status, html = await _diff(client, world.admin, world.private_journal)
    assert status == 200 and HIDDEN in html
