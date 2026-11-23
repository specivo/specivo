"""Private notes follow the View private notes permission; watcher lists carry no email addresses."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.journal import Journal
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.watcher import Watcher
from specivo.services.auth_service import _make_access_token
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@dataclass
class World:
    admin: User
    reader: User
    privileged: User
    author: User
    private_note: Journal
    own_note: Journal


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    db = db_session
    status = StatusFactory.build(name="PNW New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="PNW Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="PNW Normal", position=1, is_default=True)
    db.add_all([tracker, priority])
    project = ProjectFactory.build(key="PNW", identifier="pnw", is_public=False)
    db.add(project)
    await db.flush()
    db.add(EnabledModule(project_id=project.id, name="issue_tracking"))
    admin = AdminUserFactory.build(login="pnw_admin", status="active")
    reader = UserFactory.build(login="pnw_reader", status="active", email="reader@example.com")
    privileged = UserFactory.build(login="pnw_privileged", status="active")
    author = UserFactory.build(login="pnw_author", status="active")
    db.add_all([admin, reader, privileged, author])
    await db.flush()
    for user, perms in (
        (reader, ["view_issues"]),
        (author, ["view_issues", "add_notes"]),
        (privileged, ["view_issues", "view_private_notes"]),
    ):
        role = Role(name=f"PNW {user.login}", permissions=perms, builtin=0, issues_visibility="default")
        db.add(role)
        await db.flush()
        member = Member(user_id=user.id, project_id=project.id)
        db.add(member)
        await db.flush()
        db.add(MemberRole(member_id=member.id, role_id=role.id))
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=admin.id,
        subject="PNW issue",
    )
    db.add(issue)
    await db.flush()
    private_note = Journal(
        issue_id=issue.id, project_id=project.id, user_id=admin.id, sequence=1, notes="Managers only", is_private=True
    )
    own_note = Journal(
        issue_id=issue.id, project_id=project.id, user_id=author.id, sequence=2, notes="My own aside", is_private=True
    )
    db.add_all([private_note, own_note, Watcher(issue_id=issue.id, user_id=reader.id)])
    await db.commit()
    return World(admin, reader, privileged, author, private_note, own_note)


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


async def _notes(client: AsyncClient, user: User) -> set[str]:
    resp = await client.get("/api/v1/issues/PNW-1/", params={"include": "journals"}, headers=_auth(user))
    assert resp.status_code == 200, resp.text
    return {j["notes"] for j in resp.json()["journals"] if j.get("notes")}


async def test_view_private_notes_grants_private_notes(client: AsyncClient, world: World) -> None:
    assert await _notes(client, world.reader) == set()
    assert await _notes(client, world.privileged) == {"Managers only", "My own aside"}
    assert await _notes(client, world.admin) == {"Managers only", "My own aside"}


async def test_an_author_reads_their_own_private_note(client: AsyncClient, world: World) -> None:
    assert await _notes(client, world.author) == {"My own aside"}


async def test_reacting_to_a_private_note_follows_the_permission(client: AsyncClient, world: World) -> None:
    url = f"/api/v1/issues/PNW-1/journals/{world.private_note.id}/reactions/"
    assert (await client.get(url, headers=_auth(world.reader))).status_code == 404
    assert (await client.get(url, headers=_auth(world.privileged))).status_code == 200


async def test_watcher_lists_carry_no_email(client: AsyncClient, world: World) -> None:
    resp = await client.get("/api/v1/issues/PNW-1/watchers/", headers=_auth(world.privileged))
    assert resp.status_code == 200, resp.text
    assert resp.json() and all("email" not in w for w in resp.json())
    resp = await client.get("/api/v1/issues/PNW-1/", params={"include": "watchers"}, headers=_auth(world.privileged))
    assert "reader@example.com" not in resp.text
