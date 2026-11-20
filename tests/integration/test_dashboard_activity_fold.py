"""Grouped dashboard activity: the event sources (``activity_groups``) and folding (``_fold``)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.utils import utcnow
from specivo.models.agent_session import AgentSession
from specivo.models.journal import Journal, JournalDetail
from specivo.models.repository import IssueCommitLink, RepositoryCommit
from specivo.models.time_entry import TimeEntry, TimeEntryActivity
from specivo.models.wiki import Wiki, WikiPage
from specivo.services.api_key_service import ApiKeyService
from specivo.services.dashboard_service import ActivityEvent, _fold, activity_groups, load_scope
from tests.factories.issue import IssueFactory
from tests.factories.lookups import DoneStatusFactory, PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, ServiceAccountFactory, UserFactory
from tests.fixtures.forge_hooks import make_integration

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

BASE = utcnow()


def _event(user_id: int, target: tuple[str, int], minutes: int, *, kind: str = "comment", via: int | None = None):
    return ActivityEvent(
        ts=BASE - timedelta(minutes=minutes), kind=kind, user_id=user_id, via_key=via, target=target, text=kind
    )


def _summary(groups) -> list[tuple]:
    return [
        (
            g.actor,
            g.is_agent,
            g.is_service_account,
            g.via_client,
            g.target_kind,
            g.target_key,
            g.target_title,
            g.target_url,
            [e.ts for e in g.events],
        )
        for g in groups
    ]


async def test_fold_runs_labels_and_dropped_targets(db_session: AsyncSession) -> None:
    db = db_session
    tracker, status, priority = TrackerFactory.build(), StatusFactory.build(), PriorityFactory.build()
    person = UserFactory.build(login="fold_person", display_name="Fold Person", status="active")
    nameless = UserFactory.build(login="fold_nameless", display_name="", status="active")
    agent = ServiceAccountFactory.build(login="fold-agent", display_name="Fold Agent", status="active")
    project = ProjectFactory.build(key="FLD", identifier="fld", name="Fold")
    db.add_all([tracker, status, priority, person, nameless, agent, project])
    await db.flush()
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=4,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=person.id,
        subject="Fold subject",
    )
    wiki = Wiki(project_id=project.id)
    db.add_all([issue, wiki])
    await db.flush()
    page = WikiPage(wiki_id=wiki.id, title="Fold Page", slug="fold-page")
    db.add(page)
    keyed, _raw = await ApiKeyService().create_key(session=db, user_id=person.id, name="fold-key")
    bare, _raw = await ApiKeyService().create_key(session=db, user_id=nameless.id, name="fold-bare")
    await db.flush()
    for client_name, ago in (("old-client", 30), ("claude-code", 5)):
        db.add(
            AgentSession(
                api_key_id=keyed.id,
                user_id=person.id,
                client_name=client_name,
                client_version="1",
                started_at=BASE - timedelta(minutes=ago + 10),
                last_activity_at=BASE - timedelta(minutes=ago),
            )
        )
    await db.flush()

    on_issue, on_page = ("issue", issue.id), ("wiki", page.id)
    events = [
        _event(person.id, on_issue, 1),
        _event(person.id, on_issue, 2, kind="updated"),
        _event(person.id, on_issue, 3, via=keyed.id),  # a key starts a new run
        _event(person.id, on_issue, 4, via=keyed.id),
        _event(agent.id, on_page, 5, kind="wiki"),
        _event(nameless.id, on_issue, 6, via=bare.id),  # a key without sessions: no client
        _event(nameless.id, on_page, 7, kind="wiki"),
        _event(person.id, ("issue", 999_999_999), 8),  # a missing issue is dropped
        _event(person.id, ("wiki", 999_999_999), 9, kind="wiki"),  # a missing page is dropped
        _event(999_999_999, on_issue, 10),  # a missing user is dropped
        _event(agent.id, on_issue, 11),
        _event(agent.id, on_issue, 12),
    ]

    groups = await _fold(db, None, events, 10)  # type: ignore[arg-type]
    issue_url, page_url = "/issue/FLD-4/", "/projects/FLD/wiki/fold-page/"
    assert _summary(groups) == [
        ("Fold Person", False, False, None, "issue", "FLD-4", "Fold subject", issue_url, [events[0].ts, events[1].ts]),
        (
            "Fold Person",
            True,
            False,
            "claude-code",
            "issue",
            "FLD-4",
            "Fold subject",
            issue_url,
            [e.ts for e in events[2:4]],
        ),
        ("fold-agent", True, True, None, "wiki", "FLD", "Fold Page", page_url, [events[4].ts]),
        ("fold_nameless", True, False, "", "issue", "FLD-4", "Fold subject", issue_url, [events[5].ts]),
        ("fold_nameless", False, False, None, "wiki", "FLD", "Fold Page", page_url, [events[6].ts]),
        ("fold-agent", True, True, None, "issue", "FLD-4", "Fold subject", issue_url, [events[10].ts, events[11].ts]),
    ]

    # The limit counts runs before any is dropped; the run that would exceed it ends the fold.
    limited = await _fold(db, None, events, 3)  # type: ignore[arg-type]
    assert [(g.actor, len(g.events)) for g in limited] == [("Fold Person", 2), ("Fold Person", 2), ("fold-agent", 1)]
    assert await _fold(db, None, [], 5) == []  # type: ignore[arg-type]
    assert await _fold(db, None, events, 0) == []  # type: ignore[arg-type]

    # Only wiki targets, no keys: the issue and key lookups are skipped.
    wiki_only = await _fold(db, None, [events[4]], 5)  # type: ignore[arg-type]
    assert _summary(wiki_only) == [_summary(groups)[2]]


async def test_activity_sources_feed_the_groups(db_session: AsyncSession) -> None:
    """Journals (wiki, status, resolution, update, comment), time, created issues and matched commits, newest first."""
    db = db_session
    tracker, status, priority = TrackerFactory.build(), StatusFactory.build(name="Act Open"), PriorityFactory.build()
    done = DoneStatusFactory.build(name="Act Done")
    admin = AdminUserFactory.build(login="act_admin", display_name="Act Admin", status="active")
    dev = UserFactory.build(login="act_dev", display_name="Act Dev", status="active")
    project = ProjectFactory.build(key="ACT", identifier="act", name="Act")
    activity = TimeEntryActivity(name="Act Review", position=3)
    db.add_all([tracker, status, priority, done, admin, dev, project, activity])
    await db.flush()
    t0 = utcnow() - timedelta(hours=2)
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=dev.id,
        subject="Act subject",
        created_at=t0,
    )
    wiki = Wiki(project_id=project.id)
    db.add_all([issue, wiki])
    await db.flush()
    page = WikiPage(wiki_id=wiki.id, title="Act Page", slug="act-page")
    db.add(page)
    await db.flush()

    def journal(minutes: int, user, notes=None, *, details=(), **kw) -> None:
        j = Journal(
            issue_id=kw.pop("issue_id", issue.id),
            project_id=project.id,
            user_id=user.id,
            notes=notes,
            sequence=minutes,
            created_at=t0 + timedelta(minutes=minutes),
            **kw,
        )
        db.add(j)
        pending.append((j, details))

    pending: list = []
    journal(1, dev, details=[("status_id", str(status.id))])
    journal(2, dev, "  spaced\n  remark  ", details=[("subject", "x")])
    journal(3, dev, "private remark", is_private=True)
    journal(4, admin, issue_id=None, wiki_page_id=page.id)
    journal(5, dev, details=[("status_id", str(done.id))])
    await db.flush()
    for j, details in pending:
        for key, value in details:
            db.add(JournalDetail(journal_id=j.id, property="attr", prop_key=key, old_value=None, new_value=value))
    db.add(
        TimeEntry(
            project_id=project.id,
            issue_id=issue.id,
            user_id=dev.id,
            activity_id=activity.id,
            hours=Decimal("0.75"),
            spent_on=t0.date(),
            created_at=t0 + timedelta(minutes=6),
        )
    )
    integration = await make_integration(db, project)
    for minutes, sha, message, matched in (
        (7, "a" * 40, "First line\nmore", dev.id),
        (8, "b" * 40, "  ", dev.id),
        (9, "c" * 40, "Unmatched", None),
    ):
        row = RepositoryCommit(
            integration_id=integration.id,
            sha=sha,
            message=message,
            matched_user_id=matched,
            committed_at=t0 + timedelta(minutes=minutes),
        )
        db.add(row)
        await db.flush()
        db.add(IssueCommitLink(commit_id=row.id, issue_id=issue.id, source="message"))
    await db.flush()

    scope = await load_scope(db, admin)
    groups = await activity_groups(db, scope)

    def summary(gs) -> list[tuple]:
        return [(g.actor, g.target_key, [(e.kind, e.text, str(e.hours), e.activity) for e in g.events]) for g in gs]

    assert summary(groups) == [
        (
            "Act Dev",
            "ACT-1",
            [
                ("commit", "bbbbbbb", "0", ""),
                ("commit", "aaaaaaa First line", "0", ""),
                ("time", "", "0.75", "Act Review"),
                ("resolved", "Act Done", "0", ""),
            ],
        ),
        ("Act Admin", "ACT", [("wiki", "", "0", "")]),
        (
            "Act Dev",
            "ACT-1",
            [
                ("comment", "private remark", "0", ""),
                ("updated", "", "0", ""),
                ("comment", "spaced remark", "0", ""),
                ("status", "Act Open", "0", ""),
                ("created", "", "0", ""),
            ],
        ),
    ]
    assert summary(await activity_groups(db, scope, "agents")) == []
    assert summary(await activity_groups(db, scope, "bogus")) == summary(groups)
