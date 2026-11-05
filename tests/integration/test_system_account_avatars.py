"""Avatars of the built-in system accounts: the Git account's bundled icon, and never a ``$`` in initials."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.journal import Journal
from specivo.models.member import Member, MemberRole
from specivo.models.role import Role
from specivo.models.user import User
from specivo.web.deps import get_templates
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


def _avatar(*args: object, **kwargs: object) -> str:
    macro = get_templates().env.get_template("components/macros.html").module.avatar
    return str(macro(*args, **kwargs))


def _initials(html: str) -> str:
    match = re.search(r"<span class=\"avatar-\w+\"[^>]*>([^<]*)</span>", html)
    assert match, html
    return match.group(1)


async def test_git_account_shows_the_bundled_icon() -> None:
    html = _avatar("$git", "sm", display_name="Git")
    assert "sp-avatar-icon-git" in html
    assert 'aria-label="Git"' in html
    assert "$" not in html


async def test_git_icon_wins_over_a_colour_or_an_upload() -> None:
    html = _avatar("$git", "xs", color="#5B8C5A", avatar_url="/data/avatars/ab/abc.webp")
    assert "sp-avatar-icon-git" in html and "avatar-xs" in html
    assert "<img" not in html and "$" not in html


@pytest.mark.parametrize(
    ("login", "display_name", "expected"),
    [("$anonymous", "Anonymous", "AN"), ("$robot", "", "RO"), ("$x", "", "X")],
)
async def test_system_accounts_without_an_icon_never_show_a_dollar(
    login: str, display_name: str, expected: str
) -> None:
    html = _avatar(login, "sm", display_name=display_name)
    assert "sp-avatar-icon" not in html
    assert _initials(html) == expected


async def test_ordinary_users_keep_login_initials() -> None:
    assert _initials(_avatar("alex", "sm", display_name="Somchai Jaidee")) == "AL"


async def test_bundled_icon_is_served(client) -> None:
    resp = await client.get("/static/img/avatars/git.svg")
    assert resp.status_code == 200
    assert resp.text.lstrip().startswith("<svg")


async def test_git_journal_in_activity_uses_the_icon(client, db_session: AsyncSession) -> None:
    git = (await db_session.execute(select(User).where(User.login == "$git"))).scalar_one()
    tracker, status, priority = TrackerFactory.build(), StatusFactory.build(), PriorityFactory.build()
    reader = UserFactory.build(login="avatar_reader", status="active")
    project = ProjectFactory.build(key="AVTR", identifier="avtr", name="Avatars", is_public=False)
    db_session.add_all([tracker, status, priority, reader, project])
    await db_session.flush()
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=reader.id,
    )
    db_session.add(issue)
    await db_session.flush()
    db_session.add(
        Journal(issue_id=issue.id, project_id=project.id, user_id=git.id, notes="Linked a commit", sequence=1)
    )
    role = Role(name="avatar-reader", permissions=["view_issues"])
    db_session.add(role)
    await db_session.flush()
    member = Member(user_id=reader.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await db_session.commit()

    login = await client.post("/api/v1/auth/login/", json={"login": "avatar_reader", "password": TEST_PASSWORD})
    assert login.status_code == 200, login.text
    resp = await client.get("/issue/AVTR-1/", cookies={"access_token": login.json()["access_token"]})
    assert resp.status_code == 200, resp.text
    entry = resp.text[resp.text.index('class="journal-entry') :]
    entry = entry[: entry.index("Linked a commit")]
    assert "sp-avatar-icon-git" in entry
    assert "$G" not in resp.text


async def test_notification_from_git_shows_the_icon_in_the_header(client, db_session: AsyncSession) -> None:
    from specivo.models.notification import Notification

    git = (await db_session.execute(select(User).where(User.login == "$git"))).scalar_one()
    reader = UserFactory.build(login="avatar_notified", status="active")
    project = ProjectFactory.build(key="AVTN", identifier="avtn", name="Avatar notifications")
    db_session.add_all([reader, project])
    await db_session.flush()
    db_session.add(
        Notification(
            user_id=reader.id,
            event_type="issue_updated",
            entity_type="Issue",
            entity_id=1,
            project_id=project.id,
            actor_id=git.id,
            title="Updated by Git",
        )
    )
    await db_session.commit()

    login = await client.post("/api/v1/auth/login/", json={"login": "avatar_notified", "password": TEST_PASSWORD})
    assert login.status_code == 200, login.text
    resp = await client.get("/my/notifications/", cookies={"access_token": login.json()["access_token"]})
    assert resp.status_code == 200, resp.text
    header = resp.text[resp.text.index('class="notif-list"') :]
    header = header[: header.index("Updated by Git")]
    assert "sp-avatar-icon-git" in header
