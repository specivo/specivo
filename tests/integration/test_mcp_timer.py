"""The ``specivo_timer`` MCP tool: get, start, stop and discard the caller's timer.

Same checks as the REST timer: starting and stopping need ``log_time`` where
the timer runs (stopping re-checks and discards a timer that can no longer be
logged), and only the caller's own timer is ever read or changed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from specivo.mcp.tools import _timer
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.time_entry import ActiveTimer, TimeEntry, TimeEntryActivity
from specivo.models.user import User
from specivo.services.permission_service import clear_role_cache
from specivo.services.security_audit_service import AuditEvent
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.time_entry import ActiveTimerFactory, TimeEntryActivityFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


async def _timer_row(db: AsyncSession, user: User) -> ActiveTimer | None:
    return (await db.execute(select(ActiveTimer).where(ActiveTimer.user_id == user.id))).scalar_one_or_none()


async def _entries(db: AsyncSession, user: User) -> list[TimeEntry]:
    return list((await db.execute(select(TimeEntry).where(TimeEntry.user_id == user.id))).scalars().all())


async def _audit(db: AsyncSession, user: User, event: AuditEvent) -> list[SecurityAuditLog]:
    stmt = select(SecurityAuditLog).where(
        SecurityAuditLog.event_type == event.value, SecurityAuditLog.user_id == user.id
    )
    return list((await db.execute(stmt)).scalars().all())


@pytest_asyncio.fixture
async def activity(db_session: AsyncSession) -> TimeEntryActivity:
    act = TimeEntryActivityFactory.build(name="Mtm Review", is_default=False)
    db_session.add(act)
    await db_session.flush()
    return act


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> dict:
    status = StatusFactory.build(name="Mtm Open")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Mtm Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Mtm Normal")
    project = ProjectFactory.build(key="MTMPRJ", identifier="mtmprj", is_public=False)
    db_session.add_all([tracker, priority, project])
    await db_session.flush()

    logger_role = Role(name="Mtm Logger", permissions=["view_issues", "log_time"], builtin=0)
    reader_role = Role(name="Mtm Reader", permissions=["view_issues"], builtin=0)
    db_session.add_all([logger_role, reader_role])
    await db_session.flush()

    users = {}
    for name, role in (("logger", logger_role), ("reader", reader_role), ("other", logger_role)):
        user = UserFactory.build(login=f"mtm_{name}", status="active")
        db_session.add(user)
        await db_session.flush()
        member = Member(project_id=project.id, user_id=user.id)
        db_session.add(member)
        await db_session.flush()
        db_session.add(MemberRole(member_id=member.id, role_id=role.id))
        users[name] = user
    await db_session.flush()
    clear_role_cache(db_session)

    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=users["logger"].id,
        subject="Mtm wire the relay",
    )
    db_session.add(issue)
    await db_session.commit()
    return {"project": project, "issue": issue, "roles": {"logger": logger_role}, **users}


async def _run(db: AsyncSession, user: User, op: str, **kwargs) -> str:
    return await _timer(db, user, op, **kwargs)


def _key(issue: Issue) -> str:
    return issue.display_key


async def test_get_without_a_timer(db_session: AsyncSession, world: dict) -> None:
    assert "No timer is running" in await _run(db_session, world["logger"], "get")


async def test_start_then_get(db_session: AsyncSession, world: dict) -> None:
    issue = world["issue"]

    started = await _run(db_session, world["logger"], "start", issue_ref=_key(issue), comments="Wiring")

    assert f"Started a timer on {_key(issue)}" in started
    timer = await _timer_row(db_session, world["logger"])
    assert timer is not None and timer.issue_id == issue.id and timer.comments == "Wiring"
    assert len(await _audit(db_session, world["logger"], AuditEvent.TIMER_STARTED)) == 1

    got = await _run(db_session, world["logger"], "get")
    assert _key(issue) in got
    assert "Mtm wire the relay" in got
    assert "Elapsed: 0:00:" in got


async def test_start_requires_an_issue_ref(db_session: AsyncSession, world: dict) -> None:
    with pytest.raises(ValidationError, match="issue_ref"):
        await _run(db_session, world["logger"], "start")


async def test_start_is_refused_without_log_time(db_session: AsyncSession, world: dict) -> None:
    with pytest.raises(PermissionDeniedError, match="log_time"):
        await _run(db_session, world["reader"], "start", issue_ref=_key(world["issue"]))
    assert await _timer_row(db_session, world["reader"]) is None


async def test_stop_creates_an_entry_with_the_named_activity(
    db_session: AsyncSession, world: dict, activity: TimeEntryActivity
) -> None:
    user = world["logger"]
    db_session.add(
        ActiveTimerFactory.build(
            user_id=user.id,
            project_id=world["project"].id,
            issue_id=world["issue"].id,
            started_at=datetime.now(UTC) - timedelta(minutes=90),
        )
    )
    await db_session.commit()

    result = await _run(db_session, user, "stop", activity="mtm review", comments="Relay wired")

    assert "Logged 1.50h" in result
    [entry] = await _entries(db_session, user)
    assert entry.activity_id == activity.id
    assert entry.issue_id == world["issue"].id
    assert entry.comments == "Relay wired"
    assert await _timer_row(db_session, user) is None
    assert len(await _audit(db_session, user, AuditEvent.TIME_LOGGED)) == 1


async def test_stop_takes_an_activity_id(db_session: AsyncSession, world: dict, activity: TimeEntryActivity) -> None:
    user = world["logger"]
    db_session.add(ActiveTimerFactory.build(user_id=user.id, project_id=world["project"].id))
    await db_session.commit()

    await _run(db_session, user, "stop", activity=str(activity.id))

    [entry] = await _entries(db_session, user)
    assert entry.activity_id == activity.id


async def test_stop_refuses_an_unknown_activity(db_session: AsyncSession, world: dict) -> None:
    user = world["logger"]
    db_session.add(ActiveTimerFactory.build(user_id=user.id, project_id=world["project"].id))
    await db_session.commit()

    with pytest.raises(ValidationError, match="activity"):
        await _run(db_session, user, "stop", activity="No such activity")
    assert await _timer_row(db_session, user) is not None


async def test_stop_is_refused_without_log_time_and_discards(
    db_session: AsyncSession, world: dict, activity: TimeEntryActivity
) -> None:
    user = world["logger"]
    db_session.add(ActiveTimerFactory.build(user_id=user.id, project_id=world["project"].id))
    world["roles"]["logger"].permissions = ["view_issues"]
    await db_session.commit()
    clear_role_cache(db_session)

    with pytest.raises(PermissionDeniedError, match="log_time"):
        await _run(db_session, user, "stop", activity=str(activity.id))
    assert await _entries(db_session, user) == []
    assert await _timer_row(db_session, user) is None


async def test_stop_without_a_timer(db_session: AsyncSession, world: dict) -> None:
    with pytest.raises(NotFoundError):
        await _run(db_session, world["logger"], "stop")


async def test_discard_deletes_without_an_entry_and_audits(db_session: AsyncSession, world: dict) -> None:
    user = world["logger"]
    db_session.add(ActiveTimerFactory.build(user_id=user.id, project_id=world["project"].id))
    await db_session.commit()

    result = await _run(db_session, user, "discard")

    assert "Discarded" in result
    assert await _timer_row(db_session, user) is None
    assert await _entries(db_session, user) == []
    assert len(await _audit(db_session, user, AuditEvent.TIMER_DISCARDED)) == 1


async def test_discard_never_touches_another_users_timer(db_session: AsyncSession, world: dict) -> None:
    db_session.add(ActiveTimerFactory.build(user_id=world["other"].id, project_id=world["project"].id))
    await db_session.commit()

    with pytest.raises(NotFoundError):
        await _run(db_session, world["logger"], "discard")
    assert await _timer_row(db_session, world["other"]) is not None


async def test_an_unknown_op_is_refused(db_session: AsyncSession, world: dict) -> None:
    with pytest.raises(ValidationError, match="op"):
        await _run(db_session, world["logger"], "pause")


async def _second_issue(db: AsyncSession, world: dict) -> Issue:
    issue = IssueFactory.build(
        project_id=world["project"].id,
        project_key=world["project"].key,
        tracker_id=world["issue"].tracker_id,
        status_id=world["issue"].status_id,
        priority_id=world["issue"].priority_id,
        author_id=world["logger"].id,
        subject="Mtm second issue",
    )
    db.add(issue)
    await db.commit()
    return issue


async def test_start_reports_the_entry_logged_for_the_running_timer(
    db_session: AsyncSession, world: dict, activity: TimeEntryActivity
) -> None:
    user = world["logger"]
    other = await _second_issue(db_session, world)
    db_session.add(
        ActiveTimerFactory.build(
            user_id=user.id,
            project_id=world["project"].id,
            issue_id=other.id,
            started_at=datetime.now(UTC) - timedelta(minutes=90),
        )
    )
    await db_session.commit()

    result = await _run(db_session, user, "start", issue_ref=_key(world["issue"]))

    [entry] = await _entries(db_session, user)
    assert entry.issue_id == other.id
    assert entry.activity_id == activity.id
    assert f"Logged {entry.hours}h on {_key(other)}" in result
    assert f"Entry ID: {entry.id}" in result
    assert "discarded" not in result


async def test_start_reports_a_running_timer_that_could_not_be_logged(db_session: AsyncSession, world: dict) -> None:
    user = world["logger"]
    other = await _second_issue(db_session, world)
    other.is_private = True
    other.author_id = world["other"].id
    db_session.add(
        ActiveTimerFactory.build(
            user_id=user.id,
            project_id=world["project"].id,
            issue_id=other.id,
            started_at=datetime.now(UTC) - timedelta(minutes=90),
        )
    )
    await db_session.commit()

    result = await _run(db_session, user, "start", issue_ref=_key(world["issue"]))

    assert await _entries(db_session, user) == []
    assert "discarded" in result
    assert "no time was logged" in result
    assert "Logged" not in result
