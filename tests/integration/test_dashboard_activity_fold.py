"""Folding activity events into groups (``dashboard_service._fold``): runs, the limit, labels and dropped targets."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.utils import utcnow
from specivo.models.agent_session import AgentSession
from specivo.models.wiki import Wiki, WikiPage
from specivo.services.api_key_service import ApiKeyService
from specivo.services.dashboard_service import ActivityEvent, _fold
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import ServiceAccountFactory, UserFactory

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
