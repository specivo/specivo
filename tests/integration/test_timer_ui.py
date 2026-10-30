"""The timer in the web UI: the header context, discarding, and the stop comment.

- Every signed-in page gets the user's running timer as ``active_timer``
  (``TimeEntryService.timer_summary``, loaded once per request): the issue
  key, the subject only while the user can still see the issue, and the start
  time. Without a timer, or for a visitor without an account, there is none.
- ``POST /api/v1/timer/discard/`` drops the caller's timer without writing a
  time entry, records an audit event, and answers 404 without a timer. It
  never touches another user's timer.
- ``POST /api/v1/timer/stop/`` takes an optional comment for the entry.
- The issue page offers "Start timer" only with ``log_time``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.main import get_plugin_manager
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.time_entry import ActiveTimer, TimeEntry, TimeEntryActivity
from specivo.models.user import User
from specivo.services.permission_service import clear_role_cache
from specivo.services.security_audit_service import AuditEvent
from specivo.services.time_entry_service import TimeEntryService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.time_entry import ActiveTimerFactory, TimeEntryActivityFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_svc = TimeEntryService()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _user(db: AsyncSession, login: str) -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.flush()
    return user


async def _member(db: AsyncSession, project: Project, user: User, role: Role) -> Member:
    member = Member(project_id=project.id, user_id=user.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()
    clear_role_cache(db)
    return member


async def _headers(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _cookies(client: AsyncClient, login: str) -> dict[str, str]:
    return {"access_token": (await _headers(client, login))["Authorization"].removeprefix("Bearer ")}


async def _timer(db: AsyncSession, user: User) -> ActiveTimer | None:
    return (await db.execute(select(ActiveTimer).where(ActiveTimer.user_id == user.id))).scalar_one_or_none()


async def _entry_count(db: AsyncSession, user: User) -> int:
    return (
        await db.execute(select(func.count()).select_from(TimeEntry).where(TimeEntry.user_id == user.id))
    ).scalar_one()


async def _start(db: AsyncSession, user: User, project: Project, issue: Issue | None, minutes: int = 90) -> None:
    db.add(
        ActiveTimerFactory.build(
            user_id=user.id,
            project_id=project.id,
            issue_id=issue.id if issue is not None else None,
            started_at=datetime.now(UTC) - timedelta(minutes=minutes),
        )
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def activity(db_session: AsyncSession) -> TimeEntryActivity:
    act = TimeEntryActivityFactory.build(name="Tui Development", is_default=False)
    db_session.add(act)
    await db_session.flush()
    return act


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> dict:
    """A private project with a logger, a reader, an outsider-to-be, and one issue."""
    status = StatusFactory.build(name="Tui Open")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Tui Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Tui Normal")
    project = ProjectFactory.build(key="TUIPRJ", identifier="tuiprj", is_public=False)
    db_session.add_all([tracker, priority, project])
    await db_session.flush()

    logger_role = Role(
        name="Tui Logger",
        permissions=["view_issues", "view_time_entries", "log_time"],
        builtin=0,
        issues_visibility="default",
    )
    reader_role = Role(name="Tui Reader", permissions=["view_issues"], builtin=0, issues_visibility="default")
    db_session.add_all([logger_role, reader_role])
    await db_session.flush()

    logger = await _user(db_session, "tui_logger")
    reader = await _user(db_session, "tui_reader")
    other = await _user(db_session, "tui_other")
    await _member(db_session, project, logger, logger_role)
    await _member(db_session, project, reader, reader_role)
    await _member(db_session, project, other, logger_role)

    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=logger.id,
        subject="Tui calibrate the flux sensor",
    )
    db_session.add(issue)
    await db_session.commit()
    return {"project": project, "issue": issue, "logger": logger, "reader": reader, "other": other}


@pytest.fixture
def audit_on() -> Iterator[None]:
    """Register the enterprise audit feature (the autouse snapshot fixture removes it again)."""
    get_plugin_manager().feature_registry.register("security_audit_log", "test")
    yield


# ---------------------------------------------------------------------------
# The header context
# ---------------------------------------------------------------------------


async def test_summary_holds_the_issue_key_subject_and_start(db_session: AsyncSession, world: dict) -> None:
    await _start(db_session, world["logger"], world["project"], world["issue"])

    summary = await _svc.timer_summary(db_session, world["logger"])

    assert summary is not None
    assert summary.issue_key == world["issue"].display_key
    assert summary.issue_id == world["issue"].id
    assert summary.subject == "Tui calibrate the flux sensor"
    assert summary.project_key == "TUIPRJ"
    assert 85 * 60 <= summary.elapsed_seconds <= 95 * 60
    assert summary.started_at <= datetime.now(UTC)


async def test_summary_is_none_without_a_timer(db_session: AsyncSession, world: dict) -> None:
    assert await _svc.timer_summary(db_session, world["logger"]) is None


async def test_summary_hides_the_subject_of_an_issue_the_user_can_no_longer_see(
    db_session: AsyncSession, world: dict
) -> None:
    await _start(db_session, world["logger"], world["project"], world["issue"])
    member = (
        await db_session.execute(
            select(Member).where(Member.project_id == world["project"].id, Member.user_id == world["logger"].id)
        )
    ).scalar_one()
    await db_session.delete(member)
    await db_session.commit()
    clear_role_cache(db_session)

    summary = await _svc.timer_summary(db_session, world["logger"])

    assert summary is not None
    assert summary.issue_key == world["issue"].display_key
    assert summary.subject is None


async def test_a_signed_in_page_shows_the_running_timer(
    client: AsyncClient, db_session: AsyncSession, world: dict
) -> None:
    await _start(db_session, world["logger"], world["project"], world["issue"])

    page = await client.get("/projects/", cookies=await _cookies(client, "tui_logger"))

    assert page.status_code == 200, page.text
    assert 'x-data="headerTimer"' in page.text
    assert f'href="/issue/{world["issue"].display_key}/"' in page.text
    assert "Tui calibrate the flux sensor" in page.text


async def test_a_signed_in_page_without_a_timer_shows_none(
    client: AsyncClient, db_session: AsyncSession, world: dict
) -> None:
    page = await client.get("/projects/", cookies=await _cookies(client, "tui_logger"))

    assert page.status_code == 200, page.text
    assert 'x-data="headerTimer"' not in page.text


async def test_the_page_hides_the_subject_of_an_issue_the_user_can_no_longer_see(
    client: AsyncClient, db_session: AsyncSession, world: dict
) -> None:
    await _start(db_session, world["logger"], world["project"], world["issue"])
    member = (
        await db_session.execute(
            select(Member).where(Member.project_id == world["project"].id, Member.user_id == world["logger"].id)
        )
    ).scalar_one()
    await db_session.delete(member)
    await db_session.commit()

    page = await client.get("/projects/", cookies=await _cookies(client, "tui_logger"))

    assert page.status_code == 200, page.text
    assert 'x-data="headerTimer"' in page.text
    assert world["issue"].display_key in page.text
    assert "Tui calibrate the flux sensor" not in page.text


async def test_another_users_timer_is_not_shown(client: AsyncClient, db_session: AsyncSession, world: dict) -> None:
    await _start(db_session, world["other"], world["project"], world["issue"])

    page = await client.get("/projects/", cookies=await _cookies(client, "tui_logger"))

    assert 'x-data="headerTimer"' not in page.text


# ---------------------------------------------------------------------------
# The issue page
# ---------------------------------------------------------------------------


async def test_the_issue_page_offers_start_timer_with_log_time(
    client: AsyncClient, db_session: AsyncSession, world: dict
) -> None:
    page = await client.get(f"/issue/{world['issue'].display_key}/", cookies=await _cookies(client, "tui_logger"))

    assert page.status_code == 200, page.text
    assert 'x-data="timerStart"' in page.text
    assert 'data-running="none"' in page.text


async def test_the_issue_page_offers_no_timer_without_log_time(
    client: AsyncClient, db_session: AsyncSession, world: dict
) -> None:
    page = await client.get(f"/issue/{world['issue'].display_key}/", cookies=await _cookies(client, "tui_reader"))

    assert page.status_code == 200, page.text
    assert 'x-data="timerStart"' not in page.text


async def test_the_issue_page_knows_the_timer_runs_on_this_issue(
    client: AsyncClient, db_session: AsyncSession, world: dict
) -> None:
    await _start(db_session, world["logger"], world["project"], world["issue"])

    page = await client.get(f"/issue/{world['issue'].display_key}/", cookies=await _cookies(client, "tui_logger"))

    assert 'data-running="here"' in page.text


async def test_the_issue_page_knows_the_timer_runs_elsewhere(
    client: AsyncClient, db_session: AsyncSession, world: dict
) -> None:
    await _start(db_session, world["logger"], world["project"], None)

    page = await client.get(f"/issue/{world['issue'].display_key}/", cookies=await _cookies(client, "tui_logger"))

    assert 'data-running="other"' in page.text


# ---------------------------------------------------------------------------
# Discard
# ---------------------------------------------------------------------------


async def test_discard_deletes_the_timer_without_an_entry_and_audits(
    client: AsyncClient, db_session: AsyncSession, world: dict, audit_on: None
) -> None:
    user = world["logger"]
    await _start(db_session, user, world["project"], world["issue"])
    before = await _entry_count(db_session, user)

    resp = await client.post("/api/v1/timer/discard/", headers=await _headers(client, "tui_logger"))

    assert resp.status_code == 204, resp.text
    assert await _timer(db_session, user) is None
    assert await _entry_count(db_session, user) == before
    rows = (
        (
            await db_session.execute(
                select(SecurityAuditLog).where(
                    SecurityAuditLog.event_type == AuditEvent.TIMER_DISCARDED.value,
                    SecurityAuditLog.user_id == user.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].project_id == world["project"].id
    assert rows[0].details["issue_id"] == world["issue"].id
    assert rows[0].details["elapsed_seconds"] >= 85 * 60


async def test_discard_without_a_timer_is_404(client: AsyncClient, world: dict) -> None:
    resp = await client.post("/api/v1/timer/discard/", headers=await _headers(client, "tui_logger"))

    assert resp.status_code == 404


async def test_discard_never_touches_another_users_timer(
    client: AsyncClient, db_session: AsyncSession, world: dict
) -> None:
    await _start(db_session, world["other"], world["project"], world["issue"])

    resp = await client.post("/api/v1/timer/discard/", headers=await _headers(client, "tui_logger"))

    assert resp.status_code == 404
    assert await _timer(db_session, world["other"]) is not None


async def test_discard_requires_authentication(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/timer/discard/")

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Stop with a comment
# ---------------------------------------------------------------------------


async def test_stop_writes_the_comment_given(
    client: AsyncClient, db_session: AsyncSession, world: dict, activity: TimeEntryActivity
) -> None:
    await _start(db_session, world["logger"], world["project"], world["issue"])

    resp = await client.post(
        "/api/v1/timer/stop/",
        json={"activity_id": activity.id, "comments": "Calibrated the sensor"},
        headers=await _headers(client, "tui_logger"),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["comments"] == "Calibrated the sensor"
    assert resp.json()["activity"]["id"] == activity.id
    assert resp.json()["issue_id"] == world["issue"].id


async def test_the_summary_stays_cheap_with_a_timer_on_an_issue(db_session: AsyncSession, world: dict) -> None:
    """One query for the timer, issue and project together, plus the role lookup the visibility check needs."""
    from sqlalchemy import event

    user_id = world["logger"].id
    await _start(db_session, world["logger"], world["project"], world["issue"])
    db_session.expunge_all()
    clear_role_cache(db_session)
    user = await db_session.get(User, user_id)
    assert user is not None

    statements: list[str] = []

    def _count(conn, cursor, statement, *args) -> None:  # noqa: ANN001
        statements.append(statement)

    engine = db_session.bind.sync_engine  # type: ignore[union-attr]
    event.listen(engine, "before_cursor_execute", _count)
    try:
        summary = await _svc.timer_summary(db_session, user)
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert summary is not None and summary.subject == "Tui calibrate the flux sensor"
    assert len(statements) <= 2, statements
