"""Service tests for the completed-sprint rules and the sprint lifecycle journal.

A completed sprint's membership cannot grow by any path:

- ``complete_sprint`` refuses an unknown, other-project, completed or same-sprint
  target before changing anything.
- ``search_for_project`` (the issue sidebar picker) leaves completed sprints out;
  ``list_for_project`` keeps them.
- Creating or updating an issue into a completed sprint is refused, but
  re-sending the sprint an issue is already in is not a change, and taking an
  issue out stays allowed.

Starting or completing a sprint leaves a trace on each affected issue: a
``sprint`` detail (``started`` / ``completed``) for the issues that stay, and
a normal ``sprint_id`` change for the issues a completion moves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, ConflictError
from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.project import EnabledModule, Project
from specivo.models.sprint import Sprint
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate, IssueUpdate
from specivo.schemas.sprint import SprintCreate
from specivo.services.issue_service import IssueService
from specivo.services.sprint_service import SprintService
from tests.factories.lookups import DoneStatusFactory, PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

_svc = SprintService()
_issues = IssueService()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _project(db: AsyncSession, key: str) -> Project:
    proj = ProjectFactory.build(key=key, identifier=f"sfj-{key.lower()}")
    db.add(proj)
    await db.flush()
    db.add(EnabledModule(project_id=proj.id, name="issue_tracking"))
    await db.flush()
    return proj


async def _sprint(db: AsyncSession, project: Project, name: str) -> Sprint:
    return await _svc.create(db, project, SprintCreate(name=name))


async def _issue(
    db: AsyncSession,
    world: dict[str, Any],
    sprint: Sprint | None,
    subject: str,
    *,
    done: bool = False,
    project: Project | None = None,
) -> Issue:
    project = project or world["project"]
    project.issue_sequence += 1
    issue = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=project.issue_sequence,
        tracker_id=world["tracker"].id,
        status_id=(world["done"] if done else world["open"]).id,
        priority_id=world["priority"].id,
        author_id=world["user"].id,
        subject=subject,
        sprint_id=sprint.id if sprint else None,
    )
    db.add(issue)
    await db.flush()
    await db.refresh(issue)
    return issue


async def _details(db: AsyncSession, issue: Issue) -> list[tuple[int, str, str, str | None, str | None]]:
    """``(journal author, property, prop_key, old, new)`` for every detail of *issue*, oldest first."""
    rows = await db.execute(
        select(
            Journal.user_id,
            JournalDetail.property,
            JournalDetail.prop_key,
            JournalDetail.old_value,
            JournalDetail.new_value,
        )
        .join(Journal, Journal.id == JournalDetail.journal_id)
        .where(Journal.issue_id == issue.id)
        .order_by(Journal.sequence, JournalDetail.id)
    )
    return [tuple(r) for r in rows.all()]  # type: ignore[misc]


async def _journal_count(db: AsyncSession, issues: list[Issue]) -> int:
    stmt = select(func.count(Journal.id)).where(Journal.issue_id.in_([i.id for i in issues]))
    return int((await db.execute(stmt)).scalar_one())


async def _reload(db: AsyncSession, obj: Any) -> Any:
    await db.refresh(obj)
    return obj


@pytest.fixture
async def world(db_session: AsyncSession) -> dict[str, Any]:
    db = db_session
    user = UserFactory.build(login="sfj_lead", status="active")
    db.add(user)
    status_open = StatusFactory.build(name="SFJ New", position=1, category="backlog")
    status_done = DoneStatusFactory.build(name="SFJ Done", position=5, category="done")
    priority = PriorityFactory.build(name="SFJ Normal", is_default=True, position=1)
    db.add_all([status_open, status_done, priority])
    await db.flush()
    tracker = TrackerFactory.build(name="SFJ Task", default_status_id=status_open.id)
    db.add(tracker)
    await db.flush()
    project = await _project(db, "SFJ")
    other = await _project(db, "SFO")
    return {
        "user": user,
        "open": status_open,
        "done": status_done,
        "priority": priority,
        "tracker": tracker,
        "project": project,
        "other": other,
    }


async def _active_with_issues(db: AsyncSession, world: dict[str, Any]) -> tuple[Sprint, Issue, Issue]:
    """An active sprint holding one done and one open issue."""
    sprint = await _sprint(db, world["project"], "Sprint Alpha")
    done = await _issue(db, world, sprint, "Write the release notes", done=True)
    pending = await _issue(db, world, sprint, "Fix the login form")
    await _svc.start_sprint(db, sprint, world["user"])
    return sprint, done, pending


# ---------------------------------------------------------------------------
# 1a. complete_sprint validates its target
# ---------------------------------------------------------------------------


async def _assert_refused(
    db: AsyncSession,
    world: dict[str, Any],
    target_id: int,
    *,
    sprint: Sprint | None = None,
    issues: tuple[Issue, ...] = (),
) -> None:
    if sprint is None:
        sprint, *rest = await _active_with_issues(db, world)
        issues = tuple(rest)
    before = await _journal_count(db, list(issues))
    with pytest.raises(AppError) as exc:
        await _svc.complete_sprint(db, sprint, world["user"], move_to_sprint_id=target_id)
    assert exc.value.status_code == 422
    assert (await _reload(db, sprint)).status == "active"
    for issue in issues:
        assert (await _reload(db, issue)).sprint_id == sprint.id
    assert await _journal_count(db, list(issues)) == before


async def test_complete_rejects_completed_target(db_session: AsyncSession, world: dict[str, Any]) -> None:
    db = db_session
    old = await _sprint(db, world["project"], "Sprint Zero")
    await _svc.start_sprint(db, old, world["user"])
    await _svc.complete_sprint(db, old, world["user"])

    sprint, done, pending = await _active_with_issues(db, world)
    await _assert_refused(db, world, old.id, sprint=sprint, issues=(done, pending))


async def test_complete_rejects_other_project_target(db_session: AsyncSession, world: dict[str, Any]) -> None:
    foreign = await _sprint(db_session, world["other"], "Other Sprint")
    await _assert_refused(db_session, world, foreign.id)


async def test_complete_rejects_same_sprint_target(db_session: AsyncSession, world: dict[str, Any]) -> None:
    sprint, done, pending = await _active_with_issues(db_session, world)
    await _assert_refused(db_session, world, sprint.id, sprint=sprint, issues=(done, pending))


async def test_complete_rejects_unknown_target(db_session: AsyncSession, world: dict[str, Any]) -> None:
    await _assert_refused(db_session, world, 987654321)


async def test_complete_moves_to_planned_sprint(db_session: AsyncSession, world: dict[str, Any]) -> None:
    db = db_session
    sprint, done, pending = await _active_with_issues(db, world)
    nxt = await _sprint(db, world["project"], "Sprint Beta")

    await _svc.complete_sprint(db, sprint, world["user"], move_to_sprint_id=nxt.id)

    assert (await _reload(db, sprint)).status == "completed"
    assert (await _reload(db, done)).sprint_id == sprint.id
    assert (await _reload(db, pending)).sprint_id == nxt.id


async def test_complete_moves_to_backlog(db_session: AsyncSession, world: dict[str, Any]) -> None:
    db = db_session
    sprint, done, pending = await _active_with_issues(db, world)

    await _svc.complete_sprint(db, sprint, world["user"])

    assert (await _reload(db, done)).sprint_id == sprint.id
    assert (await _reload(db, pending)).sprint_id is None


# ---------------------------------------------------------------------------
# 1b. The picker search leaves completed sprints out
# ---------------------------------------------------------------------------


async def _three_states(db: AsyncSession, world: dict[str, Any]) -> tuple[Sprint, Sprint, Sprint]:
    completed = await _sprint(db, world["project"], "Sprint Closed One")
    await _svc.start_sprint(db, completed, world["user"])
    await _svc.complete_sprint(db, completed, world["user"])
    active = await _sprint(db, world["project"], "Sprint Running One")
    await _svc.start_sprint(db, active, world["user"])
    planned = await _sprint(db, world["project"], "Sprint Later One")
    return completed, active, planned


async def test_search_excludes_completed_sprints(db_session: AsyncSession, world: dict[str, Any]) -> None:
    completed, active, planned = await _three_states(db_session, world)

    for query in (None, "", "Sprint", "One"):
        found = {s.id for s in await _svc.search_for_project(db_session, world["project"].id, query)}
        assert found == {active.id, planned.id}, query
    assert await _svc.search_for_project(db_session, world["project"].id, "Closed") == []


async def test_list_for_project_keeps_completed_sprints(db_session: AsyncSession, world: dict[str, Any]) -> None:
    completed, active, planned = await _three_states(db_session, world)

    listed = {s.id for s in await _svc.list_for_project(db_session, world["project"].id)}
    assert listed == {completed.id, active.id, planned.id}


# ---------------------------------------------------------------------------
# 1c. Issue create / update and completed sprints
# ---------------------------------------------------------------------------


async def _completed_with_issue(db: AsyncSession, world: dict[str, Any]) -> tuple[Sprint, Issue]:
    sprint = await _sprint(db, world["project"], "Sprint Finished")
    issue = await _issue(db, world, sprint, "Ship the onboarding guide", done=True)
    await _svc.start_sprint(db, sprint, world["user"])
    await _svc.complete_sprint(db, sprint, world["user"])
    return sprint, await _reload(db, issue)


async def test_assign_to_completed_sprint_is_rejected(db_session: AsyncSession, world: dict[str, Any]) -> None:
    db = db_session
    sprint, _ = await _completed_with_issue(db, world)

    with pytest.raises(AppError) as exc:
        await _issues.create(
            db,
            world["project"],
            IssueCreate(
                project_key=world["project"].key,
                tracker_id=world["tracker"].id,
                subject="Late addition",
                sprint_id=sprint.id,
            ),
            world["user"],
        )
    assert exc.value.status_code == 422
    assert exc.value.message == "Cannot assign issues to a completed sprint"

    other = await _issue(db, world, None, "Backlog item")
    with pytest.raises(AppError) as exc:
        await _issues.update(
            db, other, IssueUpdate(lock_version=other.lock_version, sprint_id=sprint.id), world["user"]
        )
    assert exc.value.status_code == 422
    assert exc.value.message == "Cannot assign issues to a completed sprint"
    assert (await _reload(db, other)).sprint_id is None


async def test_resending_current_completed_sprint_is_allowed(db_session: AsyncSession, world: dict[str, Any]) -> None:
    db = db_session
    sprint, issue = await _completed_with_issue(db, world)
    before = await _journal_count(db, [issue])

    await _issues.update(
        db,
        issue,
        IssueUpdate(lock_version=issue.lock_version, sprint_id=sprint.id, subject="Ship the onboarding guide v2"),
        world["user"],
    )

    issue = await _reload(db, issue)
    assert issue.sprint_id == sprint.id
    assert issue.subject == "Ship the onboarding guide v2"
    changes = (await _details(db, issue))[before:]
    assert [(p, k) for _, p, k, _, _ in changes] == [("attr", "subject")]


async def test_remove_from_completed_sprint_is_allowed(db_session: AsyncSession, world: dict[str, Any]) -> None:
    db = db_session
    sprint = await _sprint(db, world["project"], "Sprint Finished")
    first = await _issue(db, world, sprint, "Ship the onboarding guide", done=True)
    second = await _issue(db, world, sprint, "Record the demo video", done=True)
    await _svc.start_sprint(db, sprint, world["user"])
    await _svc.complete_sprint(db, sprint, world["user"])
    snapshot = dict((await _reload(db, sprint)).velocity_snapshot)
    planned = await _sprint(db, world["project"], "Sprint Next")

    first = await _reload(db, first)
    await _issues.update(db, first, IssueUpdate(lock_version=first.lock_version, sprint_id=None), world["user"])
    second = await _reload(db, second)
    await _issues.update(db, second, IssueUpdate(lock_version=second.lock_version, sprint_id=planned.id), world["user"])

    assert (await _reload(db, first)).sprint_id is None
    assert (await _reload(db, second)).sprint_id == planned.id
    assert (await _reload(db, sprint)).velocity_snapshot == snapshot
    admin = AdminUserFactory.build(login="sfj_admin", status="active")
    db.add(admin)
    await db.flush()
    velocity = (await _svc.velocity_for(db, [sprint], admin))[sprint.id]
    assert velocity is not None and velocity["completed_issues"] == snapshot["completed_issues"]


# ---------------------------------------------------------------------------
# Part 2. Journal entries
# ---------------------------------------------------------------------------


async def test_start_writes_started_entry_on_each_issue(db_session: AsyncSession, world: dict[str, Any]) -> None:
    db = db_session
    sprint = await _sprint(db, world["project"], "Sprint Alpha")
    issues = [await _issue(db, world, sprint, f"Task number {n}") for n in range(3)]
    outside = await _issue(db, world, None, "Not in the sprint")

    await _svc.start_sprint(db, sprint, world["user"])

    for issue in issues:
        assert await _details(db, issue) == [(world["user"].id, "sprint", "started", str(sprint.id), "Sprint Alpha")]
    assert await _details(db, outside) == []


async def test_complete_writes_completed_entry_on_issues_that_stay(
    db_session: AsyncSession, world: dict[str, Any]
) -> None:
    db = db_session
    sprint, done, pending = await _active_with_issues(db, world)

    await _svc.complete_sprint(db, sprint, world["user"])

    assert (await _details(db, done))[-1] == (world["user"].id, "sprint", "completed", str(sprint.id), "Sprint Alpha")
    assert ("sprint", "completed") not in [(p, k) for _, p, k, _, _ in await _details(db, pending)]


@pytest.mark.parametrize("to_planned", [False, True])
async def test_complete_writes_sprint_change_on_moved_issues(
    db_session: AsyncSession, world: dict[str, Any], to_planned: bool
) -> None:
    db = db_session
    sprint, done, pending = await _active_with_issues(db, world)
    nxt = await _sprint(db, world["project"], "Sprint Beta") if to_planned else None

    await _svc.complete_sprint(db, sprint, world["user"], move_to_sprint_id=nxt.id if nxt else None)

    assert await _details(db, pending) == [
        (world["user"].id, "sprint", "started", str(sprint.id), "Sprint Alpha"),
        (world["user"].id, "attr", "sprint_id", str(sprint.id), str(nxt.id) if nxt else None),
    ]


async def test_moved_issue_lock_version_and_updated_at_advance(db_session: AsyncSession, world: dict[str, Any]) -> None:
    db = db_session
    sprint, done, pending = await _active_with_issues(db, world)
    long_ago = datetime(2020, 1, 1, tzinfo=UTC)
    pending.updated_at = long_ago
    await db.flush()
    pending = await _reload(db, pending)
    version = pending.lock_version

    await _svc.complete_sprint(db, sprint, world["user"])

    pending = await _reload(db, pending)
    assert pending.lock_version == version + 1
    assert pending.updated_at > long_ago


async def test_refused_start_or_complete_writes_nothing(db_session: AsyncSession, world: dict[str, Any]) -> None:
    db = db_session
    running, done, pending = await _active_with_issues(db, world)
    second = await _sprint(db, world["project"], "Sprint Second")
    queued = await _issue(db, world, second, "Queued work")

    with pytest.raises(ConflictError):
        await _svc.start_sprint(db, second, world["user"])
    with pytest.raises(ConflictError):
        await _svc.complete_sprint(db, second, world["user"])

    assert await _details(db, queued) == []
    assert (await _reload(db, second)).status == "planned"


async def test_empty_sprint_writes_no_journal(db_session: AsyncSession, world: dict[str, Any]) -> None:
    db = db_session
    sprint = await _sprint(db, world["project"], "Sprint Empty")
    total = int((await db.execute(select(func.count(Journal.id)))).scalar_one())

    await _svc.start_sprint(db, sprint, world["user"])
    await _svc.complete_sprint(db, sprint, world["user"])

    assert int((await db.execute(select(func.count(Journal.id)))).scalar_one()) == total
    assert (await _reload(db, sprint)).status == "completed"


async def test_webhook_only_for_moved_issues(
    db_session: AsyncSession, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import specivo.services.sprint_service as sprint_module

    emitted: list[int] = []

    async def fake_emit(session: AsyncSession, issue: Issue, journal: Journal, actor: User | None, **_: Any) -> None:
        emitted.append(issue.id)

    monkeypatch.setattr(sprint_module, "emit_issue_updated", fake_emit)
    db = db_session
    sprint, done, pending = await _active_with_issues(db, world)
    assert emitted == []

    await _svc.complete_sprint(db, sprint, world["user"])

    assert emitted == [pending.id]


async def test_complete_does_not_notify_watchers(
    db_session: AsyncSession, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from specivo.services.notification_service import NotificationService

    calls: list[Any] = []

    async def fake_notify(*args: Any, **kwargs: Any) -> None:
        calls.append(args)

    monkeypatch.setattr(NotificationService, "notify_watchers", fake_notify)
    sprint, done, pending = await _active_with_issues(db_session, world)

    await _svc.complete_sprint(db_session, sprint, world["user"])

    assert calls == []
