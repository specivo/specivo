"""AI spend per release and per sprint: the issue's version or sprint now, else its nearest ancestor's.

The world (keys ``RLA``, ``RLB``, ``RLH``; logins ``rl_*``):

- ``RLA`` (private): versions ``1.0`` and ``1.1``, a version ``1.2`` set only on a
  private parent, sprints ``Sprint 1`` and ``Sprint 2``;
- ``RLB`` (private, mgr is a member, carol is not): version ``2.0`` shared with every project;
- ``RLH`` (private, nobody but the admin): version ``9.0`` shared with every project.

The issues of ``RLA`` and the API-equivalent value of the one entry on each (USD):

- epic ``E`` (1.0, Sprint 1): 1

  - story ``A`` (none): 2, inherits 1.0 and Sprint 1

    - task ``A1`` (1.1): 4, its own version wins, Sprint 1 inherited

      - sub-task ``K`` (none): 4096, the nearest ancestor (A1) decides: 1.1
    - sub-task ``G`` (none): 2048, two levels down: 1.0, Sprint 1
  - story ``B`` (Sprint 2): 8, 1.0 inherited, its own sprint wins
- ``X`` (none): 16
- private ``PR`` (1.0), not visible to carol: 32 by alice, 1024 by carol herself
- ``Y`` (RLB's shared 2.0): 64
- ``Z`` (RLH's 9.0): 128
- private parent ``PP`` (1.2) and its public child ``C`` (none): 256 on C
- no issue: 512

``mgr`` (Manager in RLA and RLB) sees everything but RLH; ``carol`` (``view_ai_usage``
with ``all``, a plain member of RLA) cannot see the private issues or RLB.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.sprint import Sprint
from specivo.models.time_entry import TimeEntry
from specivo.models.user import User
from specivo.models.version import Version
from specivo.services.ai_spend_planning import resolve_planning
from specivo.services.ai_spend_report_service import (
    GROUP_BY,
    AiSpendReportService,
    ReportFilters,
    SpendReport,
    report_csv,
)
from specivo.services.ai_spend_summary import planning_spend
from specivo.services.nested_set_service import NestedSetService
from specivo.services.permission_service import clear_role_cache
from specivo.services.sprint_service import sprint_time
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.time_entry import TimeEntryActivityFactory
from tests.factories.user import AdminUserFactory, UserFactory
from tests.services.test_ai_spend_reports import _member, _role, entry

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_svc = AiSpendReportService()
_tree = NestedSetService()

DAY = date(2026, 3, 10)


@dataclass
class Release:
    p: Project
    q: Project
    h: Project
    users: dict[str, User]
    versions: dict[str, Version]
    sprints: dict[str, Sprint]
    issues: dict[str, Issue]


async def _issue(db: AsyncSession, project: Project, n: int, defaults: dict[str, Any], **kw: Any) -> Issue:
    issue = IssueFactory.build(project_id=project.id, project_key=project.key, sequence_number=n, **defaults, **kw)
    db.add(issue)
    await db.flush()
    return issue


async def _root(db: AsyncSession, issue: Issue) -> Issue:
    await _tree.insert_root(db, issue)
    return issue


async def _child(db: AsyncSession, parent: Issue, child: Issue) -> Issue:
    await db.refresh(parent)
    await _tree.insert_child(db, parent, child)
    await db.flush()
    return child


async def build_release(db: AsyncSession, prefix: str = "rl") -> Release:
    """The world above; *prefix* keeps keys and logins unique to the calling module (``RLA``, ``rl_mgr``)."""
    key = prefix.upper()
    status = StatusFactory.build(name=f"{prefix} Open")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name=f"{prefix} Task", default_status_id=status.id)
    priority = PriorityFactory.build(name=f"{prefix} Normal")
    p = ProjectFactory.build(key=f"{key}A", identifier=f"{prefix}a", is_public=False)
    q = ProjectFactory.build(key=f"{key}B", identifier=f"{prefix}b", is_public=False)
    h = ProjectFactory.build(key=f"{key}H", identifier=f"{prefix}h", is_public=False)
    db.add_all([tracker, priority, p, q, h])
    await db.flush()

    users = {name: UserFactory.build(login=f"{prefix}_{name}", status="active") for name in ("mgr", "alice", "carol")}
    users["admin"] = AdminUserFactory.build(login=f"{prefix}_admin", status="active")
    db.add_all(users.values())
    await db.flush()
    mgr_role = await _role(db, f"{prefix} Manager", ["*"], "all")
    viewer_role = await _role(
        db, f"{prefix} Viewer", ["view_issues", "view_ai_usage", "log_ai_usage", "view_time_entries"], "all"
    )
    for project in (p, q):
        await _member(db, project, users["mgr"], mgr_role)
    await _member(db, p, users["alice"], viewer_role)
    await _member(db, p, users["carol"], viewer_role)

    versions = {
        "1.0": Version(project_id=p.id, name="1.0"),
        "1.1": Version(project_id=p.id, name="1.1"),
        "1.2": Version(project_id=p.id, name="1.2"),
        "2.0": Version(project_id=q.id, name="2.0", sharing="system"),
        "9.0": Version(project_id=h.id, name="9.0", sharing="system"),
    }
    sprints = {
        "s1": Sprint(project_id=p.id, name="Sprint 1", status="active"),
        "s2": Sprint(project_id=p.id, name="Sprint 2"),
    }
    db.add_all([*versions.values(), *sprints.values()])
    await db.flush()
    v, s = versions, sprints
    d = {"tracker_id": tracker.id, "status_id": status.id, "priority_id": priority.id, "author_id": users["mgr"].id}

    i: dict[str, Issue] = {}
    i["E"] = await _root(
        db, await _issue(db, p, 1, d, subject="Epic", fixed_version_id=v["1.0"].id, sprint_id=s["s1"].id)
    )
    i["A"] = await _child(db, i["E"], await _issue(db, p, 2, d, subject="Story A"))
    i["A1"] = await _child(db, i["A"], await _issue(db, p, 3, d, subject="Task A1", fixed_version_id=v["1.1"].id))
    i["K"] = await _child(db, i["A1"], await _issue(db, p, 4, d, subject="Sub-task K"))
    i["G"] = await _child(db, i["A"], await _issue(db, p, 5, d, subject="Sub-task G"))
    i["B"] = await _child(db, i["E"], await _issue(db, p, 6, d, subject="Story B", sprint_id=s["s2"].id))
    i["X"] = await _root(db, await _issue(db, p, 7, d, subject="Unplanned"))
    i["PR"] = await _root(
        db, await _issue(db, p, 8, d, subject="Private", is_private=True, fixed_version_id=v["1.0"].id)
    )
    i["Y"] = await _root(db, await _issue(db, p, 9, d, subject="Shared", fixed_version_id=v["2.0"].id))
    i["Z"] = await _root(db, await _issue(db, p, 10, d, subject="Hidden release", fixed_version_id=v["9.0"].id))
    i["PP"] = await _root(
        db, await _issue(db, p, 11, d, subject="Private parent", is_private=True, fixed_version_id=v["1.2"].id)
    )
    i["C"] = await _child(db, i["PP"], await _issue(db, p, 12, d, subject="Public child"))

    alice, carol = users["alice"], users["carol"]
    for key, api in (("E", 1), ("A", 2), ("A1", 4), ("B", 8), ("X", 16), ("PR", 32), ("Y", 64), ("Z", 128)):
        await entry(db, alice, project=p, day=DAY, api=str(api), issue=i[key])
    await entry(db, alice, project=p, day=DAY, api="256", issue=i["C"])
    await entry(db, alice, project=p, day=DAY, api="512")
    await entry(db, carol, project=p, day=DAY, api="1024", issue=i["PR"])
    await entry(db, alice, project=p, day=DAY, api="2048", issue=i["G"])
    await entry(db, alice, project=p, day=DAY, api="4096", issue=i["K"])
    await db.commit()
    clear_role_cache(db)
    return Release(p=p, q=q, h=h, users=users, versions=versions, sprints=sprints, issues=i)


@pytest_asyncio.fixture
async def rl(db_session: AsyncSession) -> Release:
    return await build_release(db_session)


async def run(db: AsyncSession, viewer: User, project: Project | None, **kw: Any) -> SpendReport:
    clear_role_cache(db)
    kw.setdefault("currency", "USD")
    return await _svc.report(db, viewer, project=project, date_from=DAY, date_to=DAY, **kw)


def spend(report: SpendReport) -> dict[str | None, Decimal]:
    """API-equivalent value per row label ("" for none)."""
    out: dict[str | None, Decimal] = {}
    for row in report.rows:
        label = " / ".join(row.labels.get(d, "") for d in report.group_by)
        assert row.api_equivalent is not None
        out[label] = out.get(label, Decimal(0)) + row.api_equivalent.total.amount
    return out


def total(report: SpendReport) -> Decimal:
    assert report.totals.api_equivalent is not None
    return report.totals.api_equivalent.total.amount


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


async def test_group_by_release_uses_own_version_else_nearest_ancestor(db_session: AsyncSession, rl: Release) -> None:
    report = await run(db_session, rl.users["mgr"], rl.p, group_by=["version"])
    assert spend(report) == {
        "1.0": Decimal(1 + 2 + 8 + 32 + 1024 + 2048),
        "1.1": Decimal(4 + 4096),
        "1.2": Decimal(256),
        "RLB 2.0": Decimal(64),
        "": Decimal(16 + 128 + 512),
    }
    assert total(report) == sum(spend(report).values())
    by_id = {row.key["version"]: row for row in report.rows}
    assert set(by_id) == {rl.versions[n].id for n in ("1.0", "1.1", "1.2", "2.0")} | {None}
    # "No release" sorts last.
    assert report.rows[-1].key["version"] is None


async def test_group_by_sprint_with_inheritance_and_none(db_session: AsyncSession, rl: Release) -> None:
    report = await run(db_session, rl.users["mgr"], rl.p, group_by=["sprint"])
    rest = 16 + 32 + 1024 + 64 + 128 + 256 + 512
    assert spend(report) == {
        "Sprint 1": Decimal(1 + 2 + 4 + 2048 + 4096),
        "Sprint 2": Decimal(8),
        "": Decimal(rest),
    }


async def test_release_then_sprint(db_session: AsyncSession, rl: Release) -> None:
    report = await run(db_session, rl.users["mgr"], rl.p, group_by=["sprint", "version"])
    assert report.group_by == ("version", "sprint")
    rows = spend(report)
    assert rows["1.0 / Sprint 1"] == Decimal(1 + 2 + 2048)
    assert rows["1.0 / Sprint 2"] == Decimal(8)
    assert rows["1.1 / Sprint 1"] == Decimal(4 + 4096)
    assert rows["1.0 / "] == Decimal(32 + 1024)


async def test_instance_report_labels_every_release_with_its_project(db_session: AsyncSession, rl: Release) -> None:
    report = await run(db_session, rl.users["admin"], None, group_by=["version"])
    rows = spend(report)
    assert rows["RLA 1.0"] == Decimal(1 + 2 + 8 + 32 + 1024 + 2048)
    assert rows["RLB 2.0"] == Decimal(64)
    # The admin may open RLH, so its shared version is named.
    assert rows["RLH 9.0"] == Decimal(128)
    assert rows[""] == Decimal(16 + 512)


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


async def test_hidden_issues_and_projects_never_name_a_release(db_session: AsyncSession, rl: Release) -> None:
    carol = rl.users["carol"]
    report = await run(db_session, carol, rl.p, group_by=["version"])
    rows = spend(report)
    # PR is private: alice's 32 is not counted, carol's own 1024 counts under "No release".
    # C's version comes from a private parent; RLB and RLH are closed to carol.
    assert rows == {
        "1.0": Decimal(1 + 2 + 8 + 2048),
        "1.1": Decimal(4 + 4096),
        "": Decimal(16 + 64 + 128 + 256 + 512 + 1024),
    }
    names = {row.labels["version"] for row in report.rows}
    assert "1.2" not in names and "RLB 2.0" not in names and "RLH 9.0" not in names
    lines = list(csv.reader(io.StringIO(report_csv(report))))
    column = lines[0].index("version")
    assert {line[column] for line in lines[1:]} == {"1.0", "1.1", ""}


async def test_none_filter_matches_the_none_row_for_each_reader(db_session: AsyncSession, rl: Release) -> None:
    for name in ("mgr", "carol", "admin"):
        viewer = rl.users[name]
        project = None if name == "admin" else rl.p
        grouped = await run(db_session, viewer, project, group_by=["version"])
        none_row = next(r for r in grouped.rows if r.key["version"] is None)
        assert none_row.api_equivalent is not None
        filtered = await run(db_session, viewer, project, group_by=[], filters=ReportFilters(without_version=True))
        assert total(filtered) == none_row.api_equivalent.total.amount, name
        for row in grouped.rows:
            if row.key["version"] is None:
                continue
            one = await run(
                db_session, viewer, project, group_by=[], filters=ReportFilters(version_id=row.key["version"])
            )
            assert row.api_equivalent is not None
            assert total(one) == row.api_equivalent.total.amount, (name, row.labels)


async def test_a_release_the_reader_may_not_see_matches_nothing(db_session: AsyncSession, rl: Release) -> None:
    carol = rl.users["carol"]
    for version in ("2.0", "9.0", "1.2"):
        report = await run(
            db_session, carol, rl.p, group_by=[], filters=ReportFilters(version_id=rl.versions[version].id)
        )
        assert report.totals.entries == 0, version


async def test_sprint_filter_and_drill_facts(db_session: AsyncSession, rl: Release) -> None:
    mgr = rl.users["mgr"]
    s1 = rl.sprints["s1"].id
    report = await run(db_session, mgr, rl.p, group_by=["user"], filters=ReportFilters(sprint_id=s1))
    assert total(report) == Decimal(1 + 2 + 4 + 2048 + 4096)
    assert report.filters.excludes_seats
    facts = await _svc.drill_facts(
        db_session,
        mgr,
        project=rl.p,
        date_from=DAY,
        date_to=DAY,
        filters=ReportFilters(),
        dim="sprint",
        keys=[s1, None],
    )
    assert facts.people_by_key == {s1: 1, None: 2}


async def test_moving_an_issue_to_another_release_moves_its_spend(db_session: AsyncSession, rl: Release) -> None:
    story = rl.issues["A"]
    story.fixed_version_id = rl.versions["1.1"].id
    await db_session.commit()
    rows = spend(await run(db_session, rl.users["mgr"], rl.p, group_by=["version"]))
    # A and its sub-task G move to 1.1; A1 and K keep 1.1 anyway.
    assert rows["1.0"] == Decimal(1 + 8 + 32 + 1024)
    assert rows["1.1"] == Decimal(2 + 2048 + 4 + 4096)


# ---------------------------------------------------------------------------
# Filter values, summaries, sprint time
# ---------------------------------------------------------------------------


async def test_resolve_planning_values(db_session: AsyncSession, rl: Release) -> None:
    mgr, carol = rl.users["mgr"], rl.users["carol"]
    v = rl.versions
    assert (await resolve_planning(db_session, mgr, "version", "1.1", rl.p)).id == v["1.1"].id
    shared = await resolve_planning(db_session, mgr, "version", "2.0", rl.p)
    assert (shared.id, shared.label) == (v["2.0"].id, "RLB 2.0")
    assert (await resolve_planning(db_session, mgr, "version", str(v["2.0"].id), None)).label == "RLB 2.0"
    # Carol cannot open RLB and nobody but the admin RLH: no id, by name or by number.
    assert (await resolve_planning(db_session, carol, "version", str(v["2.0"].id), rl.p)).id == -1
    assert (await resolve_planning(db_session, carol, "version", "2.0", rl.p)).id == -1
    assert (await resolve_planning(db_session, mgr, "version", str(v["9.0"].id), rl.p)).id == -1
    sprint = await resolve_planning(db_session, carol, "sprint", "sprint 2", rl.p)
    assert (sprint.id, sprint.label) == (rl.sprints["s2"].id, "Sprint 2")
    assert (await resolve_planning(db_session, carol, "sprint", "No such sprint", rl.p)).id == -1


async def test_planning_spend_summary(db_session: AsyncSession, rl: Release) -> None:
    summary = await planning_spend(db_session, rl.users["mgr"], rl.p, ReportFilters(version_id=rl.versions["1.0"].id))
    assert summary.report is not None
    assert total(summary.report) == Decimal(1 + 2 + 8 + 32 + 1024 + 2048)
    assert summary.people == 2
    assert (summary.date_from, summary.date_to) == (DAY, DAY)
    empty = await planning_spend(db_session, rl.users["mgr"], rl.p, ReportFilters(sprint_id=-1))
    assert empty.report is None


async def test_sprint_time_follows_the_same_rule(db_session: AsyncSession, rl: Release) -> None:
    db = db_session
    i = rl.issues
    hours = {"E": "1", "A": "2", "A1": "4", "K": "8", "G": "16", "B": "32", "X": "64"}
    for key, value in hours.items():
        i[key].estimated_hours = Decimal(value)
    activity = TimeEntryActivityFactory.build(name="rl Development")
    db.add(activity)
    await db.flush()
    for key, value in (("E", "0.5"), ("G", "1.5"), ("B", "3"), ("X", "7")):
        db.add(
            TimeEntry(
                project_id=rl.p.id,
                issue_id=i[key].id,
                user_id=rl.users["alice"].id,
                activity_id=activity.id,
                hours=Decimal(value),
                spent_on=DAY,
            )
        )
    await db.commit()
    clear_role_cache(db)
    s1 = await sprint_time(db, rl.sprints["s1"], rl.users["carol"], can_view_time=True)
    assert s1.estimated == Decimal(1 + 2 + 4 + 8 + 16)
    assert s1.logged == Decimal("2.0")
    s2 = await sprint_time(db, rl.sprints["s2"], rl.users["carol"], can_view_time=False)
    assert (s2.estimated, s2.logged) == (Decimal(32), None)


async def test_group_by_lists_release_and_sprint() -> None:
    assert GROUP_BY[:2] == ("version", "sprint")
