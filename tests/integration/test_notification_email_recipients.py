"""Notification mail is written for each recipient: language, timezone, mentions, link.

A private project, and in it one issue. ``actor`` makes every change, from a
request whose locale is French, so nothing the actor's request activated may
reach the mail:

- ``thai`` (language ``th``, timezone ``Asia/Bangkok``) and ``english``
  (``en``, UTC) watch the issue;
- ``mentioned`` is a member who does not watch it;
- ``watching_mentioned`` watches it and is mentioned too;
- ``outsider`` has no membership, so cannot see the issue.

The mail is captured at ``send_notification_email.delay`` and then run
through the task with SMTP mocked, so the checks read the real MIME message.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from email import message_from_string, policy
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.i18n import activate, deactivate
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.member import Member, MemberRole
from specivo.models.notification import Notification, NotificationPreference
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.watcher import Watcher
from specivo.services.journal_service import JournalService
from specivo.services.mention_service import MentionService
from specivo.services.notification_service import NotificationService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

APP_URL = "https://specivo.example.com"
# 14:05 UTC is 21:05 in Bangkok.
NOW = datetime(2026, 11, 6, 14, 5, tzinfo=UTC)

_notifications = NotificationService()


class World:
    def __init__(self, project: Project, issue: Issue, users: dict[str, User]) -> None:
        self.project = project
        self.issue = issue
        self.users = users

    def email(self, name: str) -> str:
        return self.users[name].email


async def _user(db: AsyncSession, login: str, **fields: object) -> User:
    user: User = UserFactory.build(login=login, status="active", email=f"{login}@example.com", **fields)
    db.add(user)
    await db.flush()
    return user


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    status = StatusFactory.build(name="Ner Open")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Ner Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Ner Normal")
    project = ProjectFactory.build(key="NER", identifier="ner", is_public=False)
    role = Role(
        name="Ner Developer",
        permissions=["view_issues", "edit_issues", "add_issue_notes"],
        builtin=0,
        issues_visibility="all",
    )
    db_session.add_all([tracker, priority, project, role])
    await db_session.flush()

    users = {
        "actor": await _user(db_session, "ner_actor", display_name="Ner Actor"),
        "thai": await _user(db_session, "ner_thai", language="th", timezone="Asia/Bangkok"),
        "english": await _user(db_session, "ner_english", language="en", timezone="UTC"),
        "mentioned": await _user(db_session, "ner_mentioned"),
        "watching_mentioned": await _user(db_session, "ner_watching_mentioned"),
        "outsider": await _user(db_session, "ner_outsider"),
    }
    for name in ("actor", "thai", "english", "mentioned", "watching_mentioned"):
        member = Member(project_id=project.id, user_id=users[name].id)
        db_session.add(member)
        await db_session.flush()
        db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    issue: Issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        subject="Ner <script>alert(1)</script> & login",
        author_id=users["actor"].id,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
    )
    db_session.add(issue)
    await db_session.flush()
    for name in ("thai", "english", "watching_mentioned"):
        db_session.add(Watcher(issue_id=issue.id, user_id=users[name].id))
    await db_session.commit()
    return World(project, issue, users)


@pytest.fixture(autouse=True)
def _app_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "app_url", APP_URL)
    monkeypatch.setattr(get_settings(), "stealth_prefix", "")


@contextmanager
def _captured() -> Iterator[MagicMock]:
    """Capture queued mail, with the clock at ``NOW`` and the actor's request in French."""
    activate("fr")
    try:
        with (
            patch("specivo.tasks.notifications.send_notification_email.delay") as delay,
            patch("specivo.services.notification_service.utcnow", return_value=NOW),
        ):
            yield delay
    finally:
        deactivate()


def _messages(delay: MagicMock) -> dict[str, EmailMessage]:
    """Run each queued mail through the task with SMTP mocked; the parsed messages by recipient."""
    from specivo.tasks.notifications import send_notification_email

    messages: dict[str, EmailMessage] = {}
    for call in delay.call_args_list:
        smtp = MagicMock()
        with patch("specivo.tasks.notifications.smtplib.SMTP", return_value=smtp):
            send_notification_email(*call.args, **call.kwargs)
        message = message_from_string(smtp.sendmail.call_args.args[2], policy=policy.default)
        assert isinstance(message, EmailMessage)
        assert message["To"] not in messages, f"{message['To']} got more than one mail"
        messages[str(message["To"])] = message
    return messages


def _parts(message: EmailMessage) -> tuple[str, str]:
    text = message.get_body(preferencelist=("plain",))
    html = message.get_body(preferencelist=("html",))
    assert text is not None and html is not None
    return text.get_content(), html.get_content()


async def _comment(db: AsyncSession, world: World, notes: str) -> Journal:
    return await JournalService().add_comment(session=db, issue=world.issue, user=world.users["actor"], notes=notes)


async def _comment_with_mentions(db: AsyncSession, world: World, notes: str) -> Journal:
    """What ``POST /issues/{ref}/journals/`` does: record the mentions, then notify once."""
    journal = await _comment(db, world, notes)
    mentions = await MentionService().process_mentions(db, journal, notes, world.users["actor"], notify=False)
    await _notifications.notify_comment(
        db, world.issue, journal, world.users["actor"], mentioned_user_ids=[m.user_id for m in mentions]
    )
    return journal


async def _inbox(db: AsyncSession, world: World, name: str) -> list[Notification]:
    rows = await db.execute(
        select(Notification).where(
            Notification.user_id == world.users[name].id, Notification.entity_id == world.issue.id
        )
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# Recipient language and timezone
# ---------------------------------------------------------------------------


async def test_each_recipient_reads_their_own_language_and_timezone(db_session: AsyncSession, world: World):
    with _captured() as delay:
        await _notifications.notify_comment(
            db_session, world.issue, await _comment(db_session, world, "Ner looks good"), world.users["actor"]
        )

    messages = _messages(delay)
    thai_text, thai_html = _parts(messages[world.email("thai")])
    english_text, english_html = _parts(messages[world.email("english")])

    assert thai_text.splitlines()[0] == "Ner Actor แสดงความคิดเห็น"
    assert "21:05" in thai_text and "(Asia/Bangkok)" in thai_text
    assert "พ.ย." in thai_text  # the Thai month name
    assert "ดูความคิดเห็น" in thai_html

    assert english_text.splitlines()[0] == "Ner Actor commented"
    assert "Nov 6, 2026, 14:05 (UTC)" in english_text
    assert "View comment" in english_html
    # Nothing from the actor's French request
    assert "a commenté" not in english_text + thai_text


async def test_in_app_title_is_in_the_recipients_language(db_session: AsyncSession, world: World):
    with _captured():
        await _notifications.notify_watchers(db_session, world.issue, "status_change", world.users["actor"])

    [thai] = await _inbox(db_session, world, "thai")
    [english] = await _inbox(db_session, world, "english")
    assert thai.title == f"[{world.issue.display_key}] อัปเดตโดย Ner Actor"
    assert english.title == f"[{world.issue.display_key}] Updated by Ner Actor"


async def test_language_not_offered_falls_back_to_the_instance_default(
    db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch
):
    world.users["english"].language = "xx"
    await db_session.flush()
    monkeypatch.setattr(get_settings(), "default_language", "th")

    with _captured() as delay:
        await _notifications.notify_watchers(db_session, world.issue, "status_change", world.users["actor"])

    text, _html = _parts(_messages(delay)[world.email("english")])
    assert text.splitlines()[0] == "อัปเดตโดย Ner Actor"


# ---------------------------------------------------------------------------
# Mentions
# ---------------------------------------------------------------------------


async def test_mentioned_user_gets_a_mention_email(db_session: AsyncSession, world: World):
    with _captured() as delay:
        await _comment_with_mentions(db_session, world, "Ner please check @ner_mentioned")

    message = _messages(delay)[world.email("mentioned")]
    text, _html = _parts(message)
    assert text.splitlines()[0] == "Ner Actor mentioned you"
    assert message["X-Specivo-Event"] == "mention"
    [notification] = await _inbox(db_session, world, "mentioned")
    assert notification.event_type == "mention"
    assert notification.title == f"[{world.issue.display_key}] Ner Actor mentioned you"


async def test_mention_of_user_who_cannot_see_the_issue_sends_nothing(db_session: AsyncSession, world: World):
    with _captured() as delay:
        await _comment_with_mentions(db_session, world, "Ner ping @ner_outsider")

    assert world.email("outsider") not in _messages(delay)
    assert await _inbox(db_session, world, "outsider") == []


async def test_mention_through_mention_service_also_emails(db_session: AsyncSession, world: World):
    journal = await _comment(db_session, world, "Ner ping @ner_mentioned @ner_outsider")
    with _captured() as delay:
        await MentionService().process_mentions(db_session, journal, journal.notes or "", world.users["actor"])

    assert set(_messages(delay)) == {world.email("mentioned")}


async def test_watcher_who_is_mentioned_gets_one_mail_and_it_is_the_mention(db_session: AsyncSession, world: World):
    with _captured() as delay:
        await _comment_with_mentions(db_session, world, "Ner over to you @ner_watching_mentioned")

    messages = _messages(delay)  # fails on a second mail to the same address
    assert set(messages) == {world.email(n) for n in ("thai", "english", "watching_mentioned")}
    assert messages[world.email("watching_mentioned")]["X-Specivo-Event"] == "mention"
    assert messages[world.email("english")]["X-Specivo-Event"] == "comment"
    assert [n.event_type for n in await _inbox(db_session, world, "watching_mentioned")] == ["mention"]


async def test_mention_preferences_are_respected(db_session: AsyncSession, world: World):
    db_session.add_all(
        [
            NotificationPreference(
                user_id=world.users["mentioned"].id, project_id=None, event_type="mention", channels={"email": False}
            ),
            # A watcher who turned mention mail off still gets the comment mail.
            NotificationPreference(
                user_id=world.users["watching_mentioned"].id,
                project_id=None,
                event_type="mention",
                channels={"email": False},
            ),
        ]
    )
    await db_session.flush()

    with _captured() as delay:
        await _comment_with_mentions(db_session, world, "Ner see @ner_mentioned and @ner_watching_mentioned")

    messages = _messages(delay)
    assert world.email("mentioned") not in messages
    assert messages[world.email("watching_mentioned")]["X-Specivo-Event"] == "comment"
    # In app, the mention still arrives, once
    assert [n.event_type for n in await _inbox(db_session, world, "mentioned")] == ["mention"]
    assert [n.event_type for n in await _inbox(db_session, world, "watching_mentioned")] == ["mention"]


async def test_comment_endpoint_sends_mentions_once(client: AsyncClient, db_session: AsyncSession, world: World):
    resp = await client.post(
        "/api/v1/auth/login/", json={"login": world.users["actor"].login, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    with _captured() as delay:
        resp = await client.post(
            f"/api/v1/issues/{world.issue.display_key}/journals/",
            json={"notes": "Ner @ner_watching_mentioned @ner_mentioned @ner_outsider"},
            headers=headers,
        )
    assert resp.status_code == 201, resp.text

    messages = _messages(delay)
    assert set(messages) == {world.email(n) for n in ("thai", "english", "mentioned", "watching_mentioned")}
    assert messages[world.email("mentioned")]["X-Specivo-Event"] == "mention"
    assert messages[world.email("watching_mentioned")]["X-Specivo-Event"] == "mention"


# ---------------------------------------------------------------------------
# Link to the issue
# ---------------------------------------------------------------------------


async def test_comment_mail_links_to_the_comment(db_session: AsyncSession, world: World):
    journal = await _comment(db_session, world, "Ner a comment")
    with _captured() as delay:
        await _notifications.notify_comment(db_session, world.issue, journal, world.users["actor"])

    text, html = _parts(_messages(delay)[world.email("english")])
    url = f"{APP_URL}/issue/{world.issue.display_key}/#journal-{journal.id}"
    assert f"View comment: {url}" in text
    assert f'href="{url}"' in html


async def test_update_mail_links_to_the_issue_under_the_stealth_prefix(
    db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(get_settings(), "app_url", APP_URL + "/")
    monkeypatch.setattr(get_settings(), "stealth_prefix", "/hidden")
    with _captured() as delay:
        await _notifications.notify_watchers(db_session, world.issue, "status_change", world.users["actor"])

    text, html = _parts(_messages(delay)[world.email("english")])
    url = f"{APP_URL}/hidden/issue/{world.issue.display_key}/"
    assert f"View issue: {url}" in text
    assert f'href="{url}"' in html


async def test_local_app_url_still_links(db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "app_url", "http://localhost:8030")
    with _captured() as delay:
        await _notifications.notify_watchers(db_session, world.issue, "status_change", world.users["actor"])

    text, _html = _parts(_messages(delay)[world.email("english")])
    assert f"http://localhost:8030/issue/{world.issue.display_key}/" in text


async def test_unset_app_url_leaves_the_link_out(
    db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(get_settings(), "app_url", "")
    with _captured() as delay:
        await _notifications.notify_watchers(db_session, world.issue, "status_change", world.users["actor"])

    text, html = _parts(_messages(delay)[world.email("english")])
    assert "View issue" not in text and "View issue" not in html
    assert "href" not in html


# ---------------------------------------------------------------------------
# Content safety
# ---------------------------------------------------------------------------


async def test_user_content_is_escaped_in_html_and_kept_in_the_text_part(db_session: AsyncSession, world: World):
    journal = await _comment(db_session, world, 'Ner <img src=x onerror="alert(2)"> & <b>bold</b>')
    with _captured() as delay:
        await _notifications.notify_comment(db_session, world.issue, journal, world.users["actor"])

    message = _messages(delay)[world.email("english")]
    assert message.is_multipart()
    assert message.get_content_type() == "multipart/alternative"
    text, html = _parts(message)

    assert "<script>" not in html and "<img" not in html and "<b>bold" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; login" in html
    assert "&lt;img src=x onerror=" in html
    assert "<script>alert(1)</script> & login" in text
    assert '<img src=x onerror="alert(2)"> & <b>bold</b>' in text
    # The subject is the plain issue subject, never HTML-escaped
    assert message["Subject"] == f"[{world.issue.display_key}] Ner <script>alert(1)</script> & login"


async def test_private_issue_subject_reaches_only_people_who_can_see_it(db_session: AsyncSession, world: World):
    world.issue.is_private = True
    db_session.add(Watcher(issue_id=world.issue.id, user_id=world.users["outsider"].id))
    await db_session.flush()

    with _captured() as delay:
        await _notifications.notify_watchers(db_session, world.issue, "status_change", world.users["actor"])

    # Private issues show to members with issues_visibility 'all'; the outsider
    # still watches but gets no mail, so the subject goes nowhere else.
    assert world.email("outsider") not in _messages(delay)
