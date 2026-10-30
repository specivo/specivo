"""Integration tests: notification mail about one issue threads into one conversation.

Covers:
- assign, update and comment by three different actors: the recipient's three
  mails share Subject and References, have different Message-IDs and the
  matching X-Specivo-Event; no actor receives mail about their own action
- an issue subject with CR/LF is sent sanitised and adds no Bcc header
"""

from __future__ import annotations

from email import message_from_string, policy
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

pytestmark = pytest.mark.integration

_DELAY = "specivo.tasks.notifications.send_notification_email.delay"


async def _login(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _render(delay: MagicMock) -> list[EmailMessage]:
    """Run the queued email tasks with SMTP mocked and return the parsed messages."""
    from specivo.tasks.notifications import send_notification_email

    messages: list[EmailMessage] = []
    for call in delay.call_args_list:
        smtp = MagicMock()
        with patch("specivo.tasks.notifications.smtplib.SMTP", return_value=smtp):
            send_notification_email(*call.args, **call.kwargs)
        raw = smtp.sendmail.call_args.args[2]
        messages.append(message_from_string(raw, policy=policy.default))  # type: ignore[arg-type]
    return messages


async def _admin(db_session: AsyncSession, login: str) -> User:
    user: User = AdminUserFactory.build(login=login, status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def statuses(db_session: AsyncSession) -> tuple[IssueStatus, IssueStatus]:
    new = StatusFactory.build(name="New", position=1, category="backlog")
    doing = StatusFactory.build(name="In Progress", position=2, category="active")
    db_session.add_all([new, doing])
    await db_session.commit()
    return new, doing


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, statuses: tuple[IssueStatus, IssueStatus]) -> Tracker:
    t: Tracker = TrackerFactory.build(name="Task", default_status_id=statuses[0].id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def priority(db_session: AsyncSession) -> IssuePriority:
    p: IssuePriority = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj: Project = ProjectFactory.build(key="THR", identifier="thread-test", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def recipient(db_session: AsyncSession) -> User:
    user: User = UserFactory.build(login="thread_recipient", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _create_issue(
    client: AsyncClient,
    headers: dict[str, str],
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    subject: str,
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json={
            "project_key": project.key,
            "tracker_id": tracker.id,
            "subject": subject,
            "status_id": status.id,
            "priority_id": priority.id,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    issue: dict = resp.json()
    return issue


async def test_assign_update_comment_thread_together(
    client: AsyncClient,
    db_session: AsyncSession,
    recipient: User,
    project: Project,
    tracker: Tracker,
    statuses: tuple[IssueStatus, IssueStatus],
    priority: IssuePriority,
) -> None:
    assigner = await _admin(db_session, "thread_assigner")
    updater = await _admin(db_session, "thread_updater")
    commenter = await _admin(db_session, "thread_commenter")
    actors = {assigner.email, updater.email, commenter.email}
    as_assigner = await _login(client, assigner.login)
    as_updater = await _login(client, updater.login)
    as_commenter = await _login(client, commenter.login)
    as_recipient = await _login(client, recipient.login)

    issue = await _create_issue(client, as_assigner, project, tracker, statuses[0], priority, "Fix login")
    key = issue["key"]
    resp = await client.post(f"/api/v1/issues/{key}/watchers/", headers=as_recipient)
    assert resp.status_code in (200, 201), resp.text

    sent: list[EmailMessage] = []
    with patch(_DELAY) as delay:
        resp = await client.patch(
            f"/api/v1/issues/{key}/",
            json={"assigned_to_id": recipient.id, "lock_version": issue["lock_version"]},
            headers=as_assigner,
        )
        assert resp.status_code == 200, resp.text
    assert all(call.args[0] != assigner.email for call in delay.call_args_list)
    sent += [m for m in _render(delay) if m["To"] == recipient.email]

    with patch(_DELAY) as delay:
        resp = await client.patch(
            f"/api/v1/issues/{key}/",
            json={"status_id": statuses[1].id, "lock_version": resp.json()["lock_version"]},
            headers=as_updater,
        )
        assert resp.status_code == 200, resp.text
    assert all(call.args[0] != updater.email for call in delay.call_args_list)
    sent += [m for m in _render(delay) if m["To"] == recipient.email]

    with patch(_DELAY) as delay:
        resp = await client.post(f"/api/v1/issues/{key}/journals/", json={"notes": "Looks good"}, headers=as_commenter)
        assert resp.status_code == 201, resp.text
    assert all(call.args[0] != commenter.email for call in delay.call_args_list)
    sent += [m for m in _render(delay) if m["To"] == recipient.email]

    assert [m["X-Specivo-Event"] for m in sent] == ["assignment", "updated", "comment"]
    assert {m["Subject"] for m in sent} == {f"[{key}] Fix login"}
    assert len({m["References"] for m in sent}) == 1
    assert sent[0]["References"] == sent[0]["In-Reply-To"]
    assert sent[0]["References"].startswith("<specivo-issue-")
    assert len({m["Message-ID"] for m in sent}) == 3
    assert all(m["X-Specivo-Issue"] == key and m["X-Specivo-Project"] == project.key for m in sent)
    for msg in sent:
        plain = msg.get_body(preferencelist=("plain",))
        assert plain is not None
        values = " ".join(str(v) for _, v in msg.items())
        assert not any(actor in values for actor in actors)
    first_lines = [m.get_body(preferencelist=("plain",)).get_content().splitlines()[0] for m in sent]  # type: ignore[union-attr]
    assert first_lines == [
        f"Assigned to you by {assigner.display_name}",
        f"Updated by {updater.display_name}",
        f"{commenter.display_name} commented",
    ]


async def test_crafted_subject_is_sanitised(
    client: AsyncClient,
    db_session: AsyncSession,
    recipient: User,
    project: Project,
    tracker: Tracker,
    statuses: tuple[IssueStatus, IssueStatus],
    priority: IssuePriority,
) -> None:
    assigner = await _admin(db_session, "crafted_assigner")
    as_assigner = await _login(client, assigner.login)
    issue = await _create_issue(
        client, as_assigner, project, tracker, statuses[0], priority, "Fix login\r\nBcc: x@example.com"
    )

    with patch(_DELAY) as delay:
        resp = await client.patch(
            f"/api/v1/issues/{issue['key']}/",
            json={"assigned_to_id": recipient.id, "lock_version": issue["lock_version"]},
            headers=as_assigner,
        )
        assert resp.status_code == 200, resp.text
    [msg] = _render(delay)
    assert msg["Bcc"] is None
    assert msg["Subject"] == f"[{issue['key']}] Fix login Bcc: x@example.com"
    assert msg["To"] == recipient.email
