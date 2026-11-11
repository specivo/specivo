"""Journal and wiki-page ids in a URL are tied to the issue or project the route checked.

Reactions, thread resolution and the web wiki-trash restore checked access to
the issue or project named in the URL and then acted on any journal or page id:
a member of one project could react to (and list who reacted to) comments of a
private project, read private journals back, or restore another project's
deleted wiki pages.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.wiki import WikiPage
from specivo.services.auth_service import _make_access_token
from specivo.services.wiki_service import WikiService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@dataclass
class World:
    admin: User
    member: User
    comment: Journal
    private_comment: Journal
    foreign_comment: Journal
    foreign_page: WikiPage


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    db = db_session
    status = StatusFactory.build(name="CIS New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="CIS Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="CIS Normal", position=1, is_default=True)
    db.add_all([tracker, priority])
    await db.flush()
    mine = ProjectFactory.build(key="CIS", identifier="cis-mine", is_public=False)
    other = ProjectFactory.build(key="CIO", identifier="cis-other", is_public=False)
    db.add_all([mine, other])
    await db.flush()
    for p in (mine, other):
        db.add_all([EnabledModule(project_id=p.id, name=m) for m in ("issue_tracking", "wiki")])
    admin = AdminUserFactory.build(login="cis_admin", status="active")
    member = UserFactory.build(login="cis_member", status="active")
    db.add_all([admin, member])
    await db.flush()
    role = Role(
        name="CIS Developer",
        permissions=["view_issues", "edit_issues", "add_notes", "view_wiki", "manage_wiki", "delete_wiki_pages"],
        builtin=0,
        issues_visibility="default",
    )
    db.add(role)
    await db.flush()
    membership = Member(user_id=member.id, project_id=mine.id)
    db.add(membership)
    await db.flush()
    db.add(MemberRole(member_id=membership.id, role_id=role.id))

    def issue(project: Project) -> Issue:
        return IssueFactory.build(
            project_id=project.id,
            project_key=project.key,
            sequence_number=1,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
            author_id=member.id,
            subject=f"{project.key} issue",
        )

    my_issue, their_issue = issue(mine), issue(other)
    db.add_all([my_issue, their_issue])
    await db.flush()

    def note(i: Issue, seq: int, text: str, **kw: object) -> Journal:
        return Journal(issue_id=i.id, project_id=i.project_id, user_id=admin.id, sequence=seq, notes=text, **kw)

    comment = note(my_issue, 1, "Visible comment")
    private_comment = note(my_issue, 2, "Admins only", is_private=True)
    foreign_comment = note(their_issue, 1, "Other project secret")
    db.add_all([comment, private_comment, foreign_comment])

    page, _ = await WikiService().create_page(db, other.id, title="Other page", text="secret", author=admin)
    await WikiService().delete_page(db, page.id, deleted_by=admin)
    await db.commit()
    return World(admin, member, comment, private_comment, foreign_comment, page)


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


async def _reaction_calls(client: AsyncClient, user: User, journal: Journal) -> list[int]:
    base = f"/api/v1/issues/CIS-1/journals/{journal.id}/reactions/"
    h = _auth(user)
    return [
        (await client.post(base, json={"emoji": "thumbs_up"}, headers=h)).status_code,
        (await client.get(base, headers=h)).status_code,
        (await client.delete(f"{base}thumbs_up/", headers=h)).status_code,
        (await client.post(f"{base}heart/", headers=h)).status_code,  # the toggle the UI uses
    ]


async def test_reactions_work_on_the_issues_own_comments(client: AsyncClient, world: World) -> None:
    add, listed, removed, toggled = await _reaction_calls(client, world.member, world.comment)
    assert (add, listed, removed, toggled) == (201, 200, 204, 200)


async def test_reactions_on_another_issues_comment_are_not_found(client: AsyncClient, world: World) -> None:
    assert await _reaction_calls(client, world.member, world.foreign_comment) == [404, 404, 404, 404]


async def test_reactions_on_a_missing_journal_are_not_found(client: AsyncClient, world: World) -> None:
    missing = Journal(id=987654321)
    assert await _reaction_calls(client, world.member, missing) == [404, 404, 404, 404]


async def test_private_comment_is_hidden_from_non_admins(client: AsyncClient, world: World) -> None:
    assert await _reaction_calls(client, world.member, world.private_comment) == [404, 404, 404, 404]
    resp = await client.post(
        f"/api/v1/issues/CIS-1/journals/{world.private_comment.id}/resolve/",
        json={"summary": "done"},
        headers=_auth(world.member),
    )
    assert resp.status_code == 404
    assert "Admins only" not in resp.text
    resp = await client.post(
        f"/api/v1/issues/CIS-1/journals/{world.private_comment.id}/unresolve/", headers=_auth(world.member)
    )
    assert resp.status_code == 404

    resp = await client.post(
        f"/api/v1/issues/CIS-1/journals/{world.private_comment.id}/resolve/",
        json={"summary": "done"},
        headers=_auth(world.admin),
    )
    assert resp.status_code == 200, resp.text


async def _csrf(client: AsyncClient, user: User) -> str:
    resp = await client.get(
        "/projects/CIS/wiki/trash/", cookies={"access_token": _make_access_token(user, get_settings())}
    )
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith("csrf_token="):
            return value.split("=", 1)[1].split(";")[0]
    raise AssertionError("no csrf_token cookie")


async def test_wiki_trash_restore_is_scoped_to_the_project(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    csrf = await _csrf(client, world.member)
    resp = await client.post(
        f"/projects/CIS/wiki/trash/{world.foreign_page.id}/restore/",
        data={"csrf_token": csrf},
        cookies={"access_token": _make_access_token(world.member, get_settings()), "csrf_token": csrf},
        headers={"x-csrf-token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 404, resp.text
    await db_session.refresh(world.foreign_page)
    assert world.foreign_page.deleted_at is not None
