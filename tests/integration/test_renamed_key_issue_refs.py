"""Issue references under a project's old key keep working after a key rename.

Renaming ``RNMO`` to ``RNMN`` re-keys every issue, so ``RNMO-1`` names no
current issue; ``project_key_aliases`` remembers that ``RNMO`` was this
project. The web page redirects to the current key, the REST API and MCP
return the issue under it, and an issue the reader may not see stays exactly
as hidden as a reference that names nothing.

Chains are covered too: a project renamed twice, an issue moved out before
the rename, and one moved out after it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.issue_service import IssueService
from specivo.services.project_service import ProjectService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@dataclass
class World:
    project: Project
    other: Project
    admin: User
    member: User
    issues: dict[int, Issue]


@pytest_asyncio.fixture
async def world(admin_client: AsyncClient, auth_client: AsyncClient, db_session: AsyncSession) -> World:
    """``RNMO`` with issues 1 to 3 (2 is private, by the admin) and ``RNMX`` to move issues into.

    The ``auth_client`` user is a member of both with a role that sees every
    issue that is not private.
    """
    tracker, status, priority = TrackerFactory.build(), StatusFactory.build(), PriorityFactory.build()
    db_session.add_all([tracker, status, priority])
    project = ProjectFactory.build(key="RNMO", identifier="rnmo", name="Rename me", is_public=False)
    other = ProjectFactory.build(key="RNMX", identifier="rnmx", name="Elsewhere", is_public=False)
    db_session.add_all([project, other])
    await db_session.flush()

    admin: User = admin_client.state.user  # type: ignore[attr-defined]
    member: User = auth_client.state.user  # type: ignore[attr-defined]
    role = Role(
        name="Rename ref reader",
        permissions=["view_issues", "view_project", "edit_issues"],
        builtin=0,
        issues_visibility="default",
    )
    db_session.add(role)
    await db_session.flush()
    for p in (project, other):
        membership = Member(user_id=member.id, project_id=p.id)
        db_session.add(membership)
        await db_session.flush()
        db_session.add(MemberRole(member_id=membership.id, role_id=role.id))

    issues: dict[int, Issue] = {}
    for number in (1, 2, 3):
        issue = IssueFactory.build(
            project_id=project.id,
            project_key=project.key,
            sequence_number=number,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
            author_id=admin.id,
            subject=f"Renamed issue {number}",
            is_private=number == 2,
        )
        db_session.add(issue)
        await db_session.flush()
        issues[number] = issue
    project.issue_sequence = 3
    await db_session.commit()
    return World(project, other, admin, member, issues)


async def _rename(db: AsyncSession, world: World, new_key: str) -> None:
    project = await db.get(Project, world.project.id)
    assert project is not None
    await ProjectService().rename(db, project, new_key, None, world.admin)
    await db.commit()


async def _move(db: AsyncSession, world: World, issue: Issue) -> Issue:
    current = await db.get(Issue, issue.id)
    assert current is not None
    moved = await IssueService().move(db, current, world.other, world.admin)
    await db.commit()
    return moved


class TestWeb:
    async def test_issue_page_redirects_to_the_current_key(self, auth_client, db_session, world):
        await _rename(db_session, world, "RNMN")

        resp = await auth_client.get("/issue/RNMO-1/?activity_page=2&tab=time", follow_redirects=False)

        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == "/issue/RNMN-1/?activity_page=2&tab=time"
        followed = await auth_client.get(resp.headers["location"])
        assert followed.status_code == 200
        assert "Renamed issue 1" in followed.text

    async def test_edit_page_redirects_to_the_current_key(self, auth_client, db_session, world):
        await _rename(db_session, world, "RNMN")

        resp = await auth_client.get("/issue/rnmo-1/edit/", follow_redirects=False)

        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == "/issue/RNMN-1/edit/"

    async def test_current_key_is_not_redirected(self, auth_client, db_session, world):
        await _rename(db_session, world, "RNMN")

        resp = await auth_client.get("/issue/RNMN-1/", follow_redirects=False)

        assert resp.status_code == 200

    async def test_private_issue_under_the_old_key_is_a_plain_404(self, auth_client, db_session, world):
        await _rename(db_session, world, "RNMN")

        hidden = await auth_client.get("/issue/RNMO-2/", follow_redirects=False)
        unknown = await auth_client.get("/issue/RNMO-99/", follow_redirects=False)

        assert hidden.status_code == unknown.status_code == 404
        assert hidden.text == unknown.text.replace("RNMO-99", "RNMO-2")


class TestRest:
    async def test_old_key_returns_the_issue_under_its_current_key(self, auth_client, db_session, world):
        await _rename(db_session, world, "RNMN")

        resp = await auth_client.get("/api/v1/issues/RNMO-1/")

        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == world.issues[1].id
        assert resp.json()["key"] == "RNMN-1"

    async def test_other_issue_routes_accept_the_old_key(self, auth_client, db_session, world):
        await _rename(db_session, world, "RNMN")

        resp = await auth_client.get("/api/v1/issues/RNMO-1/relations/")

        assert resp.status_code == 200, resp.text

    async def test_private_issue_under_the_old_key_matches_an_unknown_key(self, auth_client, db_session, world):
        await _rename(db_session, world, "RNMN")

        hidden = await auth_client.get("/api/v1/issues/RNMO-2/")
        unknown = await auth_client.get("/api/v1/issues/RNMO-98/")

        assert hidden.status_code == unknown.status_code == 404
        assert hidden.text == unknown.text.replace("RNMO-98", "RNMO-2")

    async def test_admin_sees_the_private_issue_under_the_old_key(self, admin_client, db_session, world):
        await _rename(db_session, world, "RNMN")

        resp = await admin_client.get("/api/v1/issues/RNMO-2/")

        assert resp.status_code == 200
        assert resp.json()["key"] == "RNMN-2"

    async def test_unknown_old_key_is_404(self, auth_client, db_session, world):
        await _rename(db_session, world, "RNMN")

        assert (await auth_client.get("/api/v1/issues/RNMO-4/")).status_code == 404
        assert (await auth_client.get("/api/v1/issues/NOSUCHKEY-1/")).status_code == 404

    async def test_project_renamed_twice(self, auth_client, db_session, world):
        await _rename(db_session, world, "RNMN")
        await _rename(db_session, world, "RNMZ")

        for ref in ("RNMO-3", "RNMN-3", "RNMZ-3"):
            resp = await auth_client.get(f"/api/v1/issues/{ref}/")
            assert resp.status_code == 200, (ref, resp.text)
            assert resp.json()["key"] == "RNMZ-3"

    async def test_issue_moved_out_before_the_rename(self, auth_client, db_session, world):
        moved = await _move(db_session, world, world.issues[1])
        await _rename(db_session, world, "RNMN")

        resp = await auth_client.get("/api/v1/issues/RNMO-1/")

        assert resp.status_code == 200, resp.text
        assert resp.json()["key"] == moved.display_key == "RNMX-1"
        # RNMN-1 never existed: the issue had left before the project was renamed.
        assert (await auth_client.get("/api/v1/issues/RNMN-1/")).status_code == 404

    async def test_issue_moved_out_after_the_rename(self, auth_client, db_session, world):
        await _rename(db_session, world, "RNMN")
        moved = await _move(db_session, world, world.issues[3])

        for ref in ("RNMO-3", "RNMN-3"):
            resp = await auth_client.get(f"/api/v1/issues/{ref}/")
            assert resp.status_code == 200, (ref, resp.text)
            assert resp.json()["key"] == moved.display_key == "RNMX-1"

    async def test_issue_moved_between_two_renames(self, auth_client, db_session, world):
        await _rename(db_session, world, "RNMN")
        moved = await _move(db_session, world, world.issues[3])
        await _rename(db_session, world, "RNMZ")

        for ref in ("RNMO-3", "RNMN-3"):
            resp = await auth_client.get(f"/api/v1/issues/{ref}/")
            assert resp.status_code == 200, (ref, resp.text)
            assert resp.json()["key"] == moved.display_key


class TestMcp:
    async def test_show_issue_with_the_old_key(self, db_session, world):
        from specivo.mcp.tools import _show_issue

        await _rename(db_session, world, "RNMN")

        text = await _show_issue(db_session, world.member, "RNMO-1")

        assert "Issue: RNMN-1" in text

    async def test_show_issue_hides_a_private_issue_like_an_unknown_one(self, db_session, world):
        from specivo.core.exceptions import NotFoundError
        from specivo.mcp.tools import _show_issue

        await _rename(db_session, world, "RNMN")

        with pytest.raises(NotFoundError) as hidden:
            await _show_issue(db_session, world.member, "RNMO-2")
        with pytest.raises(NotFoundError) as unknown:
            await _show_issue(db_session, world.member, "RNMO-97")
        assert str(hidden.value).replace("RNMO-2", "X") == str(unknown.value).replace("RNMO-97", "X")


class TestAutolinks:
    async def test_old_key_in_text_resolves_as_a_known_ref(self, db_session, world):
        await _rename(db_session, world, "RNMN")

        known = await IssueService().resolve_known_issue_refs(
            db_session, world.member, "See RNMO-1, RNMO-2 and RNMO-99."
        )

        assert known == {"RNMO-1"}
