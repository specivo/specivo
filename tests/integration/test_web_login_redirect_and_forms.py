"""Web pages send visitors back after login, and issue forms need the permission they save with."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> tuple[Project, Issue, User, User]:
    db = db_session
    status = StatusFactory.build(name="WLR New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="WLR Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="WLR Normal", position=1, is_default=True)
    db.add_all([tracker, priority])
    project = ProjectFactory.build(key="WLR", identifier="wlr", is_public=False)
    db.add(project)
    await db.flush()
    db.add(EnabledModule(project_id=project.id, name="issue_tracking"))
    reader = UserFactory.build(login="wlr_reader", status="active")
    editor = UserFactory.build(login="wlr_editor", status="active")
    db.add_all([reader, editor])
    await db.flush()
    for user, perms in ((reader, ["view_issues"]), (editor, ["view_issues", "add_issues", "edit_issues"])):
        role = Role(name=f"WLR {user.login}", permissions=perms, builtin=0, issues_visibility="default")
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
        author_id=editor.id,
        subject="WLR issue",
    )
    db.add(issue)
    await db.commit()
    return project, issue, reader, editor


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def test_a_page_sends_the_visitor_back_after_login(unauth_client: AsyncClient, world) -> None:
    resp = await unauth_client.get("/issue/WLR-1/edit/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login/?next=%2Fissue%2FWLR-1%2Fedit%2F"


async def test_a_partial_goes_to_the_login_page_alone(unauth_client: AsyncClient, world) -> None:
    resp = await unauth_client.get("/partials/issues/WLR-1/activity/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login/"


@pytest.mark.parametrize("url", ["/projects/WLR/issues/new/", "/issue/WLR-1/edit/"])
async def test_issue_forms_need_the_permission_they_save_with(client: AsyncClient, world, url: str) -> None:
    _project, _issue, reader, editor = world
    resp = await client.get(url, cookies=_cookies(reader), follow_redirects=False)
    assert resp.status_code == 403, resp.text
    resp = await client.get(url, cookies=_cookies(editor), follow_redirects=False)
    assert resp.status_code == 200, resp.text
