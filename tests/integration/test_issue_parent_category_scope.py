"""An issue's parent and category must come from its own project, and the parent must be visible.

Before, the REST create and update loaded the parent by id alone and wrote
``category_id`` unchecked: a member of one project could hang subtasks under
another project's private issue (changing its rolled-up progress) and read
other projects' category names back from the response.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.issue import Issue
from specivo.models.lookups import IssueCategory
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


@dataclass
class World:
    member: User
    tracker_id: int
    mine: Project
    own_issue: Issue
    hidden_issue: Issue
    foreign_issue: Issue
    own_category: IssueCategory
    foreign_category: IssueCategory


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    db = db_session
    status = StatusFactory.build(name="PCS New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="PCS Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="PCS Normal", position=1, is_default=True)
    db.add_all([tracker, priority])
    await db.flush()

    mine = ProjectFactory.build(key="PCS", identifier="pcs-mine", is_public=False)
    other = ProjectFactory.build(key="PCO", identifier="pcs-other", is_public=False)
    db.add_all([mine, other])
    await db.flush()
    db.add_all([EnabledModule(project_id=p.id, name="issue_tracking") for p in (mine, other)])

    admin = AdminUserFactory.build(login="pcs_admin", status="active")
    member = UserFactory.build(login="pcs_member", status="active")
    db.add_all([admin, member])
    await db.flush()
    role = Role(
        name="PCS Developer",
        permissions=["view_issues", "add_issues", "edit_issues", "manage_subtasks"],
        builtin=0,
        issues_visibility="default",
    )
    db.add(role)
    await db.flush()
    membership = Member(user_id=member.id, project_id=mine.id)
    db.add(membership)
    await db.flush()
    db.add(MemberRole(member_id=membership.id, role_id=role.id))

    def issue(project: Project, seq: int, *, private: bool = False) -> Issue:
        return IssueFactory.build(
            project_id=project.id,
            project_key=project.key,
            sequence_number=seq,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
            author_id=admin.id,
            subject=f"{project.key} issue {seq}",
            is_private=private,
        )

    own, hidden, foreign = issue(mine, 1), issue(mine, 2, private=True), issue(other, 1)
    db.add_all([own, hidden, foreign])
    await db.flush()
    for root in (own, hidden, foreign):
        root.root_id = root.id  # each a tree of its own, as insert_root leaves it
    own_category = IssueCategory(project_id=mine.id, name="Mine category")
    foreign_category = IssueCategory(project_id=other.id, name="Secret category")
    db.add_all([own_category, foreign_category])
    mine.issue_sequence = 2
    other.issue_sequence = 1
    await db.commit()
    return World(member, tracker.id, mine, own, hidden, foreign, own_category, foreign_category)


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


async def _create(client: AsyncClient, w: World, **fields: object):
    body = {"project_key": "PCS", "tracker_id": w.tracker_id, "subject": "New work", **fields}
    return await client.post("/api/v1/projects/PCS/issues/", json=body, headers=_auth(w.member))


async def _patch(client: AsyncClient, w: World, **fields: object):
    resp = await client.get("/api/v1/issues/PCS-1/", headers=_auth(w.member))
    assert resp.status_code == 200, resp.text
    body = {"lock_version": resp.json()["lock_version"], **fields}
    return await client.patch("/api/v1/issues/PCS-1/", json=body, headers=_auth(w.member))


async def test_parent_in_the_same_project_still_works(client: AsyncClient, world: World) -> None:
    resp = await _create(client, world, parent_id=world.own_issue.id, category_id=world.own_category.id)
    assert resp.status_code == 201, resp.text
    assert resp.json()["parent_id"] == world.own_issue.id


async def test_parent_from_another_project_is_not_found(client: AsyncClient, world: World) -> None:
    resp = await _create(client, world, parent_id=world.foreign_issue.id)
    assert resp.status_code == 404, resp.text
    # The same answer as for an id that does not exist at all.
    assert (await _create(client, world, parent_id=987654)).status_code == 404


async def test_parent_the_caller_cannot_see_is_not_found(client: AsyncClient, world: World) -> None:
    assert (await client.get("/api/v1/issues/PCS-2/", headers=_auth(world.member))).status_code == 404
    resp = await _create(client, world, parent_id=world.hidden_issue.id)
    assert resp.status_code == 404, resp.text


async def test_update_cannot_move_under_a_foreign_or_hidden_parent(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    assert (await _patch(client, world, parent_id=world.foreign_issue.id)).status_code == 404
    assert (await _patch(client, world, parent_id=world.hidden_issue.id)).status_code == 404
    await db_session.refresh(world.own_issue)
    assert world.own_issue.parent_id is None


async def test_category_from_another_project_is_refused(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    resp = await _create(client, world, category_id=world.foreign_category.id)
    assert resp.status_code == 422, resp.text
    assert "Secret category" not in resp.text

    resp = await _patch(client, world, category_id=world.foreign_category.id)
    assert resp.status_code == 422, resp.text
    assert "Secret category" not in resp.text
    await db_session.refresh(world.own_issue)
    assert world.own_issue.category_id is None

    resp = await _patch(client, world, category_id=world.own_category.id)
    assert resp.status_code == 200, resp.text
