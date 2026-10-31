"""Outgoing webhook events are written to the outbox by the services and dispatched after commit."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.utils import utcnow
from specivo.models.webhook import WebhookDelivery
from specivo.schemas.issue import IssueCreate, IssueUpdate
from specivo.services import webhook_events
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from specivo.services.webhook_delivery import claim_due
from specivo.services.webhook_service import WebhookService
from specivo.services.wiki_service import WikiService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_issues = IssueService()
_journals = JournalService()
_webhooks = WebhookService()
_wiki = WikiService()
_APPLY_ASYNC = "specivo.tasks.webhooks.deliver_webhook.apply_async"


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession):
    status = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    t = TrackerFactory.build(name="Bug", default_status_id=status.id)
    db_session.add(t)
    db_session.add(PriorityFactory.build(name="Normal", is_default=True, position=2))
    await db_session.commit()
    return t


@pytest_asyncio.fixture
async def project(db_session: AsyncSession):
    proj = ProjectFactory.build(key="WHEMIT", name="Webhook Emit", identifier="whemit")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession):
    user = AdminUserFactory.build(login="whemit_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _hook(db_session: AsyncSession, project, events, **kwargs):
    webhook, _secret = await _webhooks.register(
        db_session, project.id, "https://hooks.example.com/in", events=events, **kwargs
    )
    await db_session.commit()
    return webhook


async def _deliveries(db_session: AsyncSession, event: str | None = None) -> list[WebhookDelivery]:
    stmt = select(WebhookDelivery).order_by(WebhookDelivery.id)
    if event is not None:
        stmt = stmt.where(WebhookDelivery.event == event)
    return list((await db_session.execute(stmt)).scalars().all())


async def _issue(db_session: AsyncSession, project, tracker, admin, **kwargs):
    data = IssueCreate(project_key=project.key, tracker_id=tracker.id, subject="Login fails", **kwargs)
    return await _issues.create(db_session, project, data, admin)


class TestIssueCreated:
    async def test_subscribed_webhook_gets_one_pending_delivery(self, db_session, project, tracker, admin):
        webhook = await _hook(db_session, project, ["issue.created"])
        issue = await _issue(db_session, project, tracker, admin)

        rows = await _deliveries(db_session)
        assert len(rows) == 1
        row = rows[0]
        assert row.webhook_id == webhook.id
        assert row.status == "pending"
        assert row.attempts == 0
        assert row.payload["event"] == "issue.created"
        assert row.payload["delivery_id"] == str(row.delivery_uuid)
        assert row.payload["project"] == {"key": "WHEMIT", "name": "Webhook Emit"}
        assert row.payload["actor"]["login"] == "whemit_admin"
        assert row.payload["data"]["issue"]["key"] == issue.display_key
        assert row.payload["data"]["issue"]["status"]["name"] == "New"

    async def test_unsubscribed_event_inserts_none(self, db_session, project, tracker, admin):
        await _hook(db_session, project, ["wiki.page.updated"])
        await _issue(db_session, project, tracker, admin)
        assert await _deliveries(db_session) == []

    async def test_inactive_webhook_inserts_none(self, db_session, project, tracker, admin):
        webhook = await _hook(db_session, project, ["issue.created"])
        await _webhooks.update(db_session, webhook.id, project.id, {"is_active": False})
        await db_session.commit()
        await _issue(db_session, project, tracker, admin)
        assert await _deliveries(db_session) == []

    async def test_one_delivery_per_subscribed_webhook(self, db_session, project, tracker, admin):
        await _hook(db_session, project, ["issue.created"])
        await _hook(db_session, project, ["issue.created", "issue.updated"])
        await _hook(db_session, project, ["issue.updated"])
        await _issue(db_session, project, tracker, admin)
        rows = await _deliveries(db_session)
        assert len(rows) == 2
        assert len({r.delivery_uuid for r in rows}) == 2

    async def test_rollback_leaves_none(self, db_session, project, tracker, admin):
        await _hook(db_session, project, ["issue.created"])
        with patch(_APPLY_ASYNC) as apply_async:
            await _issue(db_session, project, tracker, admin)
            await db_session.rollback()
            assert await _deliveries(db_session) == []
            await db_session.commit()
        apply_async.assert_not_called()

    async def test_private_issue_skipped_without_include_private(self, db_session, project, tracker, admin):
        public_only = await _hook(db_session, project, ["issue.created"])
        with_private = await _hook(db_session, project, ["issue.created"], include_private=True)
        await _issue(db_session, project, tracker, admin, is_private=True)
        rows = await _deliveries(db_session)
        assert [r.webhook_id for r in rows] == [with_private.id]
        assert public_only.id not in [r.webhook_id for r in rows]

    async def test_new_webhook_seen_after_cached_empty_list(self, db_session, project, tracker, admin):
        assert await webhook_events.active_subscriptions(db_session, project.id) == []
        await _hook(db_session, project, ["issue.created"])
        await _issue(db_session, project, tracker, admin)
        assert len(await _deliveries(db_session)) == 1

    async def test_suppressed_session_emits_nothing(self, db_session, project, tracker, admin):
        await _hook(db_session, project, ["issue.created"])
        webhook_events.suppress_webhook_events(db_session)
        try:
            await _issue(db_session, project, tracker, admin)
        finally:
            db_session.info.pop(webhook_events.SUPPRESS_KEY, None)
        assert await _deliveries(db_session) == []


class TestDispatch:
    async def test_after_commit_dispatches_new_ids(self, db_session, project, tracker, admin):
        await _hook(db_session, project, ["issue.created"])
        with patch(_APPLY_ASYNC) as apply_async:
            await _issue(db_session, project, tracker, admin)
            ids = [r.id for r in await _deliveries(db_session)]
            apply_async.assert_not_called()
            await db_session.commit()
        assert [call.kwargs["args"] for call in apply_async.call_args_list] == [[i, 0] for i in ids]

    async def test_soft_rollback_dispatches_nothing(self, db_session, project, tracker, admin):
        await _hook(db_session, project, ["issue.created"])
        with patch(_APPLY_ASYNC) as apply_async:
            await _issue(db_session, project, tracker, admin)
            await db_session.rollback()
            await db_session.commit()
        apply_async.assert_not_called()

    async def test_broker_down_leaves_row_pending_for_the_sweeper(self, db_session, project, tracker, admin):
        await _hook(db_session, project, ["issue.created"])
        with patch(_APPLY_ASYNC, side_effect=ConnectionError("broker down")):
            await _issue(db_session, project, tracker, admin)
            await db_session.commit()
        (row,) = await _deliveries(db_session)
        assert row.status == "pending"

        # Not due yet: the lease given at commit time still holds.
        assert await claim_due(db_session) == []
        due = await claim_due(db_session, now=row.next_attempt_at + timedelta(seconds=1))
        assert due == [(row.id, 0)]
        await db_session.refresh(row)
        assert row.next_attempt_at > utcnow()

        with patch(_APPLY_ASYNC) as apply_async:
            assert webhook_events.dispatch_deliveries(due) == 1
        apply_async.assert_called_once_with(args=[row.id, 0], retry=False)


class TestIssueUpdated:
    async def test_update_sends_change_set(self, db_session, project, tracker, admin):
        issue = await _issue(db_session, project, tracker, admin)
        await db_session.commit()
        await _hook(db_session, project, ["issue.updated"])

        await _issues.update(
            db_session,
            issue,
            IssueUpdate(lock_version=issue.lock_version, subject="Login fails on Safari", done_ratio=30),
            admin,
            notes="Narrowed down",
        )
        (row,) = await _deliveries(db_session)
        data = row.payload["data"]
        assert row.event == "issue.updated"
        assert data["changes"] == {
            "subject": {"old": "Login fails", "new": "Login fails on Safari"},
            "done_ratio": {"old": "0", "new": "30"},
        }
        assert data["notes"] == "Narrowed down"
        assert data["issue"]["subject"] == "Login fails on Safari"
        assert isinstance(data["journal_id"], int)

    async def test_no_op_update_sends_nothing(self, db_session, project, tracker, admin):
        issue = await _issue(db_session, project, tracker, admin)
        await db_session.commit()
        await _hook(db_session, project, ["issue.updated"])
        await _issues.update(db_session, issue, IssueUpdate(lock_version=issue.lock_version), admin)
        assert await _deliveries(db_session) == []

    async def test_making_an_issue_private_is_private(self, db_session, project, tracker, admin):
        issue = await _issue(db_session, project, tracker, admin)
        await db_session.commit()
        await _hook(db_session, project, ["issue.updated"])
        await _issues.update(db_session, issue, IssueUpdate(lock_version=issue.lock_version, is_private=True), admin)
        assert await _deliveries(db_session) == []

    async def test_making_an_issue_public_is_private(self, db_session, project, tracker, admin):
        issue = await _issue(db_session, project, tracker, admin, is_private=True)
        await db_session.commit()
        await _hook(db_session, project, ["issue.updated"])
        await _issues.update(db_session, issue, IssueUpdate(lock_version=issue.lock_version, is_private=False), admin)
        assert await _deliveries(db_session) == []


class TestIssueCommented:
    async def test_comment(self, db_session, project, tracker, admin):
        issue = await _issue(db_session, project, tracker, admin)
        await db_session.commit()
        await _hook(db_session, project, ["issue.commented"])
        journal = await _journals.add_comment(db_session, issue, admin, "Looking into it")

        (row,) = await _deliveries(db_session)
        assert row.event == "issue.commented"
        assert row.payload["data"]["comment"]["id"] == journal.id
        assert row.payload["data"]["comment"]["notes"] == "Looking into it"
        assert row.payload["data"]["issue"]["id"] == issue.id

    async def test_private_note_skipped_without_include_private(self, db_session, project, tracker, admin):
        issue = await _issue(db_session, project, tracker, admin)
        await db_session.commit()
        await _hook(db_session, project, ["issue.commented"])
        journal = await _journals.add_comment(db_session, issue, admin, "Internal")
        for row in await _deliveries(db_session):
            await db_session.delete(row)
        journal.is_private = True
        await webhook_events.emit_issue_commented(db_session, issue, journal, admin)
        assert await _deliveries(db_session) == []


class TestIssueDeleted:
    async def test_delete_sends_issue_as_it_was(self, db_session, project, tracker, admin):
        issue = await _issue(db_session, project, tracker, admin)
        await db_session.commit()
        key = issue.display_key
        await _hook(db_session, project, ["issue.deleted"])
        await _issues.delete(db_session, issue, actor=admin)

        (row,) = await _deliveries(db_session)
        assert row.event == "issue.deleted"
        assert row.payload["data"]["issue"]["key"] == key
        assert row.payload["actor"]["login"] == "whemit_admin"


class TestWikiPageUpdated:
    async def test_create_and_update(self, db_session, project, admin):
        await _hook(db_session, project, ["wiki.page.updated"])
        page, _content = await _wiki.create_page(db_session, project.id, "Release notes", "v1", admin)
        await db_session.commit()
        await _wiki.update_page(db_session, page.id, "v2", admin, lock_version=page.lock_version, comment="Typo")

        rows = await _deliveries(db_session)
        assert [r.event for r in rows] == ["wiki.page.updated", "wiki.page.updated"]
        first, second = (r.payload["data"] for r in rows)
        assert first["created"] is True
        assert first["page"]["slug"] == "release-notes"
        assert first["page"]["content_version"] == 1
        assert second["created"] is False
        assert second["page"]["text"] == "v2"
        assert second["page"]["content_version"] == 2
        assert second["comments"] == "Typo"

    async def test_auto_created_home_page_sends_nothing(self, db_session, project, admin):
        await _hook(db_session, project, ["wiki.page.updated"])
        await _wiki.ensure_home_page(db_session, project.id, admin)
        assert await _deliveries(db_session) == []
