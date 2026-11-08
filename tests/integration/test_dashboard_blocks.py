"""The dashboard blocks: attention counts, agents working now, AI spend and agent time, grouped activity.

The world of the spend report tests (``tests.services.test_ai_spend_reports``)
with its own prefix: private projects ``DBA`` and ``DBB``. ``mgr`` manages DBA,
``alice`` and ``bob`` are developers in both (they see the private issue only
when they are its author or assignee), ``dave`` is in neither, ``admin`` is an
administrator. Usage entries are dated today (UTC), so they fall in every
period of the switch.
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.utils import utcnow
from specivo.models.agent_session import AgentSession
from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.lookups import IssueStatus
from specivo.models.relation import IssueRelation
from specivo.models.time_entry import ActiveTimer, TimeEntry, TimeEntryActivity
from specivo.models.user import User
from specivo.services.api_key_service import ApiKeyService
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import clear_role_cache
from tests.factories.issue import IssueFactory
from tests.factories.lookups import ClosedStatusFactory, StatusFactory
from tests.factories.user import ServiceAccountFactory
from tests.services.test_ai_spend_reports import World, build_world, entry

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

TODAY = utcnow().date()


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    world = await build_world(db_session, "db")
    clear_role_cache(db_session)
    return world


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def _get(client: AsyncClient, user: User, url: str = "/", **params: str) -> Response:
    resp = await client.get(url, params=params, cookies=_cookies(user), follow_redirects=False)
    assert resp.status_code == 200, resp.text[:500]
    return resp


def _text(resp: Response) -> str:
    return " ".join(resp.text.split())


def _block(resp: Response, testid: str) -> str:
    """The HTML of the section with ``data-testid=testid`` (up to its closing tag)."""
    text = _text(resp)
    match = re.search(rf'<section[^>]*data-testid="{testid}".*?</section>', text)
    assert match is not None, testid
    return match.group(0)


def _kpi(resp: Response, name: str) -> int:
    match = re.search(rf'data-testid="dash-kpi-{name}"> <span class="sp-dash-kpi-value">(\d+)</span>', _text(resp))
    assert match is not None, name
    return int(match.group(1))


async def _issue(db: AsyncSession, w: World, seq: int, *, status: IssueStatus | None = None, **kw) -> Issue:
    template = w.public_issue
    issue = IssueFactory.build(
        project_id=w.p.id,
        project_key=w.p.key,
        sequence_number=seq,
        tracker_id=template.tracker_id,
        status_id=status.id if status else template.status_id,
        priority_id=template.priority_id,
        author_id=w.mgr.id,
        **kw,
    )
    db.add(issue)
    await db.flush()
    return issue


async def _note(db: AsyncSession, issue: Issue, user: User, notes: str | None = None, **kw) -> Journal:
    seq = (
        await db.execute(select(func.coalesce(func.max(Journal.sequence), 0)).where(Journal.issue_id == issue.id))
    ).scalar_one()
    journal = Journal(
        issue_id=issue.id, project_id=issue.project_id, user_id=user.id, notes=notes, sequence=seq + 1, **kw
    )
    db.add(journal)
    await db.flush()
    return journal


async def _session(db: AsyncSession, user: User, issue: Issue | None, *, idle: timedelta = timedelta(minutes=2)):
    key, _raw = await ApiKeyService().create_key(
        session=db, user_id=user.id, name=f"{user.login}-{utcnow().timestamp()}"
    )
    now = utcnow()
    row = AgentSession(
        api_key_id=key.id,
        user_id=user.id,
        issue_id=issue.id if issue else None,
        client_name="claude-code",
        client_version="2.1.0",
        started_at=now - idle - timedelta(minutes=20),
        last_activity_at=now - idle,
    )
    db.add(row)
    await db.flush()
    return row


# ---------------------------------------------------------------------------
# Needs your attention
# ---------------------------------------------------------------------------


async def test_attention_counts_open_feedback_overdue_and_blocked(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    db = db_session
    feedback = StatusFactory.build(name="Feedback", category="active")
    closed = ClosedStatusFactory.build(name="db Closed")
    db.add_all([feedback, closed])
    await db.flush()
    await _issue(db, w, 10, assigned_to_id=w.alice.id, subject="Plain open")
    await _issue(db, w, 11, assigned_to_id=w.alice.id, status=feedback, subject="Waiting on alice")
    await _issue(db, w, 12, assigned_to_id=w.alice.id, due_date=TODAY - timedelta(days=2), subject="Late one")
    blocked = await _issue(db, w, 13, assigned_to_id=w.alice.id, subject="Waits on another")
    no_longer = await _issue(db, w, 14, assigned_to_id=w.alice.id, subject="Its blocker is closed")
    await _issue(db, w, 15, assigned_to_id=w.alice.id, status=closed, due_date=TODAY - timedelta(days=9))
    blocker = await _issue(db, w, 16, subject="Blocker")
    done_blocker = await _issue(db, w, 17, status=closed, subject="Closed blocker")
    db.add_all(
        [
            IssueRelation(issue_from_id=blocker.id, issue_to_id=blocked.id, relation_type="blocks"),
            IssueRelation(issue_from_id=done_blocker.id, issue_to_id=no_longer.id, relation_type="blocks"),
        ]
    )
    await db.commit()

    resp = await _get(client, w.alice)
    assert _kpi(resp, "open") == 5
    assert _kpi(resp, "feedback") == 1
    assert _kpi(resp, "overdue") == 1
    assert _kpi(resp, "blocked") == 1
    issues = _block(resp, "dash-my-issues")
    assert issues.count('data-testid="dash-flag-blocked"') == 1
    assert issues.count('data-testid="dash-flag-overdue"') == 1

    # Someone else's issues count for no one else.
    resp = await _get(client, w.bob)
    assert [_kpi(resp, k) for k in ("open", "feedback", "overdue", "blocked")] == [0, 0, 0, 0]


async def test_a_blocker_the_reader_cannot_see_does_not_count(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    blocked = await _issue(db_session, w, 20, assigned_to_id=w.alice.id)
    db_session.add(IssueRelation(issue_from_id=w.private_issue.id, issue_to_id=blocked.id, relation_type="blocks"))
    await db_session.commit()
    assert _kpi(await _get(client, w.alice), "blocked") == 0
    w.private_issue.assigned_to_id = w.alice.id
    await db_session.commit()
    assert _kpi(await _get(client, w.alice), "blocked") == 1


async def test_my_issues_partial_counts_only_visible_issues(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    """An issue assigned to the reader that they can no longer see (private, not theirs) stays out."""
    w.private_issue.assigned_to_id = w.bob.id
    await _issue(db_session, w, 30, assigned_to_id=w.bob.id, subject="Bob visible work")
    await db_session.commit()
    resp = await _get(client, w.bob, "/partials/dashboard/my-issues/")
    assert "Bob visible work" in resp.text and "Private work" in resp.text  # assignee sees it
    w.private_issue.is_private = True
    w.private_issue.assigned_to_id = w.alice.id
    await db_session.commit()
    resp = await _get(client, w.bob, "/partials/dashboard/my-issues/")
    assert "Private work" not in resp.text


# ---------------------------------------------------------------------------
# Agents working now
# ---------------------------------------------------------------------------


async def test_agents_working_now_lists_only_the_viewers_own_sessions(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    db = db_session
    await _session(db, w.alice, w.public_issue)
    await _session(db, w.bob, w.private_issue)  # bob cannot see the private issue
    await _session(db, w.dave, None)
    await _session(db, w.mgr, None, idle=timedelta(hours=2))  # closed: idle past the timeout
    await db.commit()

    alice = _block(await _get(client, w.alice), "dash-agents")
    assert alice.count('data-testid="dash-session"') == 1
    assert w.public_issue.display_key in alice and "Public work" in alice
    assert "claude-code 2.1.0" in alice
    assert "Rep Bob" not in alice  # a co-member's session is not listed

    # Bob's own session is listed, without the issue he cannot see.
    bob = _block(await _get(client, w.bob), "dash-agents")
    assert bob.count('data-testid="dash-session"') == 1 and "Rep Bob" in bob
    assert "Rep Alice" not in bob
    assert w.private_issue.display_key not in bob and "No issue yet" in bob

    # mgr manages DBA but sees no one else's session; their own is closed.
    mgr = _block(await _get(client, w.mgr), "dash-agents")
    assert 'data-testid="dash-agents-empty"' in mgr

    # The administrator sees every active session, and not the closed one.
    admin = _block(await _get(client, w.admin), "dash-agents")
    assert admin.count('data-testid="dash-session"') == 3
    assert 'data-testid="dash-agents-count">3 active' in admin
    assert w.private_issue.display_key in admin


async def test_agents_working_now_shows_the_running_timer_and_the_service_account(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    db = db_session
    agent = ServiceAccountFactory.build(login="db-build-agent", status="active")
    db.add(agent)
    await db.flush()
    await _session(db, agent, w.public_issue)
    db.add(
        ActiveTimer(
            user_id=agent.id, project_id=w.p.id, issue_id=w.public_issue.id, started_at=utcnow() - timedelta(minutes=84)
        )
    )
    await db.commit()
    admin = _block(await _get(client, w.admin), "dash-agents")
    assert "db-build-agent" in admin and "Service account" in admin
    assert re.search(r"Timer 1:2[45]", admin)


async def test_agents_working_now_empty_state(client: AsyncClient, w: World) -> None:
    block = _block(await _get(client, w.alice), "dash-agents")
    assert 'data-testid="dash-agents-empty"' in block
    assert "No agents active right now." in block


# ---------------------------------------------------------------------------
# AI spend & agent time
# ---------------------------------------------------------------------------


async def _spend_world(db: AsyncSession, w: World) -> None:
    await entry(db, w.alice, project=w.p, day=TODAY, api="30", cost_type="subscription", subscription=w.subs["alice"])
    await entry(db, w.bob, project=w.p, day=TODAY, api="4", model="lab-model-2")
    await entry(db, w.bob, project=w.q, day=TODAY, api=None, model="lab-model-3")  # unpriced
    await db.commit()


async def test_a_member_gets_no_spend_block(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    await _spend_world(db_session, w)
    for user in (w.bob, w.dave):
        resp = await _get(client, user)
        assert 'data-testid="dash-spend"' not in resp.text, user.login
        assert "lab-model" not in resp.text
        partial = await client.get("/partials/dashboard/spend/", cookies=_cookies(user), follow_redirects=False)
        assert partial.status_code == 403, user.login
        assert "lab-model" not in partial.text


async def test_a_manager_sees_the_spend_of_the_projects_they_manage(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    """mgr reads everyone's AI usage in DBA only: DBA's entries and seat share, nothing of DBB."""
    await _spend_world(db_session, w)
    block = _block(await _get(client, w.mgr), "dash-spend")
    assert "lab-model-1" in block and "lab-model-2" in block
    assert "lab-model-3" not in block  # bob's entry in DBB
    assert w.q.key not in block
    assert "1 usage entry has no known price" not in block
    assert 'data-testid="dash-spend-leverage"' in block and "seats +" in block  # alice's share in DBA
    assert "AI usage and hours of the project where you see everyone" in block
    assert f'href="/projects/{w.p.key}/ai-usage/report/?preset=custom' in block
    assert "/admin/ai-spend/" not in block
    partial = await _get(client, w.mgr, "/partials/dashboard/spend/", period="7d")
    assert "lab-model-2" in partial.text and "lab-model-3" not in partial.text


async def test_view_ai_usage_with_own_visibility_is_no_manager(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    """alice and bob hold View AI usage with the "own" visibility in both projects: no block, partial refused."""
    await _spend_world(db_session, w)
    for user in (w.alice, w.bob):
        resp = await _get(client, user)
        assert 'data-testid="dash-spend"' not in resp.text, user.login
        partial = await client.get("/partials/dashboard/spend/", cookies=_cookies(user), follow_redirects=False)
        assert partial.status_code == 403


async def test_the_admin_sees_every_entry_and_the_full_report_link(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    await _spend_world(db_session, w)
    block = _block(await _get(client, w.admin), "dash-spend")
    for model in ("lab-model-1", "lab-model-2", "lab-model-3"):
        assert model in block
    assert w.p.key in block and w.q.key in block
    assert "All AI usage on this instance" in block
    assert 'href="/admin/ai-spend/?preset=custom' in block
    assert "1 usage entry has no known price" in block
    assert 'data-testid="dash-spend-leverage"' in block and "seats +" in block
    assert "Secret plan" not in block


async def test_spend_empty_state(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    for sub in w.subs.values():
        await db_session.delete(sub)
    await db_session.commit()
    block = _block(await _get(client, w.admin), "dash-spend")
    assert 'data-testid="dash-spend-empty"' in block


async def test_the_period_switch(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    await entry(db_session, w.bob, project=w.p, day=TODAY - timedelta(days=3), api="7")
    await db_session.commit()

    today = await _get(client, w.admin, "/partials/dashboard/spend/", period="today")
    assert 'data-period="today"' in today.text
    assert "lab-model-1" not in today.text
    assert re.search(r'aria-pressed="true" data-testid="dash-period-today"', today.text)
    assert re.search(r'aria-pressed="false" data-testid="dash-period-7d"', today.text)

    week = await _get(client, w.admin, "/partials/dashboard/spend/", period="7d")
    assert "lab-model-1" in week.text and 'data-period="7d"' in week.text

    # The page takes the same parameter; an unknown period reads as the month.
    page = _block(await _get(client, w.admin, period="7d"), "dash-spend")
    assert 'data-period="7d"' in page
    page = _block(await _get(client, w.admin, period="decade"), "dash-spend")
    assert 'data-period="month"' in page


async def test_agent_and_people_hours(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    db = db_session
    agent = ServiceAccountFactory.build(login="db-hours-agent", status="active")
    activity = TimeEntryActivity(name="db Development", position=1)
    db.add_all([agent, activity])
    await db.flush()
    for user, hours in ((agent, "2.5"), (w.mgr, "1.0")):
        db.add(
            TimeEntry(
                project_id=w.p.id,
                issue_id=w.public_issue.id,
                user_id=user.id,
                activity_id=activity.id,
                hours=Decimal(hours),
                spent_on=TODAY,
            )
        )
    await db.commit()
    block = _block(await _get(client, w.admin), "dash-spend")
    assert 'data-testid="dash-spend-agent-hours">2.5 h' in block
    assert 'data-testid="dash-spend-people-hours">People: 1.0 h' in block


# ---------------------------------------------------------------------------
# Grouped activity
# ---------------------------------------------------------------------------


async def test_consecutive_events_fold_into_one_group(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    db = db_session
    agent = ServiceAccountFactory.build(login="db-review-agent", status="active")
    done = StatusFactory.build(name="db Shipped", category="done")
    activity = TimeEntryActivity(name="db Testing", position=2)
    db.add_all([agent, done, activity])
    await db.flush()
    base = utcnow() - timedelta(hours=1)
    issue = w.public_issue
    for minutes, notes in ((1, None), (2, "first remark"), (3, None)):
        journal = await _note(db, issue, agent, notes, created_at=base + timedelta(minutes=minutes))
        if notes is None:
            db.add(
                JournalDetail(journal_id=journal.id, property="attr", prop_key="subject", old_value="a", new_value="b")
            )
    db.add(
        TimeEntry(
            project_id=w.p.id,
            issue_id=issue.id,
            user_id=agent.id,
            activity_id=activity.id,
            hours=Decimal("1.5"),
            spent_on=TODAY,
            created_at=base + timedelta(minutes=4),
        )
    )
    closing = await _note(db, issue, agent, created_at=base + timedelta(minutes=5))
    db.add(
        JournalDetail(
            journal_id=closing.id, property="attr", prop_key="status_id", old_value=None, new_value=str(done.id)
        )
    )
    # A person comments afterwards: a group of their own.
    await _note(db, issue, w.mgr, "person remark", created_at=base + timedelta(minutes=6))
    await db.commit()

    resp = await _get(client, w.mgr)
    groups = re.findall(r'<details class="sp-dash-group".*?</details>', _text(resp))
    agent_group = next(g for g in groups if "db-review-agent" in g)
    assert "2 updates, 1 comment, 1.5h logged (db Testing), resolved" in agent_group
    assert agent_group.count('class="sp-dash-event ') == 5
    assert "first remark" in agent_group and "Resolved (db Shipped)" in agent_group
    assert "sp-dash-tag--agent" in agent_group
    person = next(g for g in groups if "person remark" in g)
    assert "Rep Mgr" in person and "person remark" in person and "Person" in person
    assert groups[0].startswith('<details class="sp-dash-group" data-testid="dash-activity-group" open>')
    assert not agent_group.startswith('<details class="sp-dash-group" data-testid="dash-activity-group" open>')


async def test_activity_filter_chips(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    db = db_session
    agent = ServiceAccountFactory.build(login="db-chip-agent", status="active")
    db.add(agent)
    await db.flush()
    key, _raw = await ApiKeyService().create_key(session=db, user_id=w.mgr.id, name="db-mgr-key")
    base = utcnow() - timedelta(minutes=30)
    await _note(db, w.public_issue, agent, "agent remark", created_at=base)
    await _note(db, w.public_issue, w.mgr, "keyed remark", api_key_id=key.id, created_at=base + timedelta(minutes=1))
    await _note(db, w.public_issue, w.mgr, "hand remark", created_at=base + timedelta(minutes=2))
    await db.commit()

    def remarks(resp: Response) -> set[str]:
        return {r for r in ("agent remark", "keyed remark", "hand remark") if r in resp.text}

    assert remarks(await _get(client, w.mgr, "/partials/dashboard/activity/", who="all")) == {
        "agent remark",
        "keyed remark",
        "hand remark",
    }
    agents = await _get(client, w.mgr, "/partials/dashboard/activity/", who="agents")
    assert remarks(agents) == {"agent remark", "keyed remark"}
    assert "via an API key" in agents.text
    assert re.search(r'aria-pressed="true" data-testid="dash-activity-agents"', agents.text)
    people = await _get(client, w.mgr, "/partials/dashboard/activity/", who="people")
    assert remarks(people) == {"hand remark"}
    page = await _get(client, w.mgr, who="people")
    assert 'data-filter="people"' in page.text


async def test_activity_leaves_out_hidden_issues_and_private_notes(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    db = db_session
    await _note(db, w.private_issue, w.mgr, "hidden issue remark")
    await _note(db, w.public_issue, w.mgr, "private remark", is_private=True)
    await _note(db, w.public_issue, w.mgr, "public remark")
    await db.commit()
    alice = await _get(client, w.alice)
    assert "public remark" in alice.text
    assert "hidden issue remark" not in alice.text and "private remark" not in alice.text
    admin = await _get(client, w.admin)
    assert "hidden issue remark" in admin.text and "private remark" in admin.text


async def test_activity_empty_state(client: AsyncClient, w: World) -> None:
    block = _block(await _get(client, w.dave), "dash-activity")
    assert 'data-testid="dash-activity-empty"' in block


async def test_a_member_without_projects_gets_every_block(client: AsyncClient, w: World) -> None:
    resp = await _get(client, w.dave)
    for testid in ("dash-attention", "dash-agents", "dash-my-issues", "dash-activity", "dash-projects"):
        _block(resp, testid)
    assert "No projects yet." in resp.text


async def test_partials_need_a_signed_in_user(client: AsyncClient) -> None:
    for url in ("/partials/dashboard/spend/", "/partials/dashboard/activity/", "/partials/dashboard/my-issues/"):
        resp = await client.get(url, follow_redirects=False)
        assert resp.status_code == 302 and resp.headers["location"] == "/login/"
