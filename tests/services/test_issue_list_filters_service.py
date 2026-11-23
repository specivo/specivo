"""IssueService.list_issues: every filter key, the status sentinels and the sort grammar.

Three issues with distinct values in every filtered column; each case names
the subjects it must return, in order. Listed by an administrator, so the
visibility rules (covered by the listing visibility tests) stay out of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError
from specivo.models.lookups import IssueStatus
from specivo.models.project import Project
from specivo.models.tag import Tag, TagLink
from specivo.models.user import User
from specivo.services.issue_service import IssueService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import ClosedStatusFactory, PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory
from tests.factories.version import VersionFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_svc = IssueService()


def _at(month: int, day: int) -> datetime:
    return datetime(2026, month, day, 12, 0, tzinfo=UTC)


@dataclass
class World:
    project: Project
    admin: User
    ids: dict[str, int]
    closed: IssueStatus


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    db = db_session
    open_status = StatusFactory.build(name="Lif Open")
    closed = ClosedStatusFactory.build(name="Lif Closed")
    db.add_all([open_status, closed])
    await db.flush()
    t1 = TrackerFactory.build(name="Lif Bug", default_status_id=open_status.id)
    t2 = TrackerFactory.build(name="Lif Task", default_status_id=open_status.id)
    p1 = PriorityFactory.build(name="Lif Low")
    db.add_all([t1, t2, p1])
    await db.flush()
    p2 = PriorityFactory.build(name="Lif High")
    project = ProjectFactory.build(key="LIF", identifier="lif")
    admin = AdminUserFactory.build(login="lif_admin", status="active")
    bob = UserFactory.build(login="lif_bob", status="active")
    db.add_all([p2, project, admin, bob])
    await db.flush()
    version = VersionFactory.build(project_id=project.id, name="lif-1")
    tag = Tag(project_id=project.id, name="lif-tag")
    db.add_all([version, tag])
    await db.flush()

    def issue(n: int, subject: str, **kw: Any) -> Any:
        values: dict[str, Any] = {
            "status_id": open_status.id,
            "priority_id": p1.id,
            "tracker_id": t1.id,
            "author_id": admin.id,
            **kw,
        }
        return IssueFactory.build(
            project_id=project.id, project_key=project.key, sequence_number=n, subject=subject, **values
        )

    alpha = issue(
        1,
        "Alpha login",
        assigned_to_id=bob.id,
        fixed_version_id=version.id,
        issue_metadata={"team": "core", "labels": ["x", "y"]},
        created_at=_at(1, 1),
        updated_at=_at(1, 5),
    )
    db.add(alpha)
    await db.flush()
    beta = issue(
        2,
        "Beta 100% done",
        status_id=closed.id,
        priority_id=p2.id,
        tracker_id=t2.id,
        author_id=bob.id,
        parent_id=alpha.id,
        issue_metadata={"team": "web"},
        created_at=_at(2, 1),
        updated_at=_at(2, 10),
    )
    gamma = issue(3, "Gamma_under", priority_id=p2.id, created_at=_at(3, 1), updated_at=_at(3, 3))
    db.add_all([beta, gamma])
    await db.flush()
    db.add(TagLink(tag_id=tag.id, issue_id=alpha.id))
    await db.commit()
    ids = {
        "alpha": alpha.id,
        "t1": t1.id,
        "t2": t2.id,
        "p2": p2.id,
        "bob": bob.id,
        "version": version.id,
        "tag": tag.id,
    }
    return World(project=project, admin=admin, ids=ids, closed=closed)


async def _subjects(db: AsyncSession, w: World, filters: dict, sort: str = "", offset: int = 0, limit: int = 50):
    issues, total = await _svc.list_issues(db, w.project.id, filters, sort, offset, limit, w.admin)
    return [i.subject.split()[0].split("_")[0] for i in issues], total


async def test_every_filter_key(db_session: AsyncSession, w: World) -> None:
    i = w.ids
    every = {"status": "all"}
    cases: list[tuple[dict[str, Any], list[str]]] = [
        ({}, ["Gamma", "Alpha"]),
        ({"status": "open"}, ["Gamma", "Alpha"]),
        ({"status": "closed"}, ["Beta"]),
        ({"status": None}, ["Gamma", "Beta", "Alpha"]),
        (every, ["Gamma", "Beta", "Alpha"]),
        ({"status": str(w.closed.id)}, ["Beta"]),
        ({"status": " lif CLOSED "}, ["Beta"]),
        ({**every, "tracker_id": i["t1"]}, ["Gamma", "Alpha"]),
        ({**every, "assigned_to_id": "none"}, ["Gamma", "Beta"]),
        ({**every, "assigned_to_id": i["bob"]}, ["Alpha"]),
        ({**every, "priority_id": i["p2"]}, ["Gamma", "Beta"]),
        ({**every, "category_id": 999_999}, []),
        ({**every, "author_id": i["bob"]}, ["Beta"]),
        ({**every, "version_id": "none"}, ["Gamma", "Beta"]),
        ({**every, "version_id": i["version"]}, ["Alpha"]),
        ({**every, "sprint_id": 999_999}, []),
        ({**every, "tag_id": i["tag"]}, ["Alpha"]),
        ({**every, "parent_id": i["alpha"]}, ["Beta"]),
        ({**every, "subject_contains": "100%"}, ["Beta"]),
        ({**every, "subject_contains": "a_u"}, ["Gamma"]),
        ({**every, "subject_contains": "ALPHA"}, ["Alpha"]),
        ({**every, "subject_contains": ""}, ["Gamma", "Beta", "Alpha"]),
        ({**every, "created_after": _at(1, 15), "created_before": _at(2, 15)}, ["Beta"]),
        ({**every, "created_after": _at(3, 1)}, ["Gamma"]),
        ({**every, "updated_after": _at(1, 6), "updated_before": _at(3, 3)}, ["Gamma", "Beta"]),
        ({**every, "metadata_filters": [("team", "core")]}, ["Alpha"]),
        ({**every, "metadata_filters": [("labels", "y")]}, ["Alpha"]),
        ({**every, "metadata_filters": [("team", "web"), ("labels", "y")]}, []),
        ({**every, "metadata_filters": []}, ["Gamma", "Beta", "Alpha"]),
        ({**every, "tracker_id": None, "author_id": None, "metadata_filters": None}, ["Gamma", "Beta", "Alpha"]),
    ]
    for filters, expected in cases:
        subjects, total = await _subjects(db_session, w, filters)
        assert (subjects, total) == (expected, len(expected)), filters


async def test_an_unknown_status_name_is_refused(db_session: AsyncSession, w: World) -> None:
    with pytest.raises(AppError):
        await _subjects(db_session, w, {"status": "no such status"})


async def test_sort_grammar_and_paging(db_session: AsyncSession, w: World) -> None:
    every = {"status": "all"}
    cases: list[tuple[str, list[str]]] = [
        ("", ["Gamma", "Beta", "Alpha"]),
        ("subject:asc", ["Alpha", "Beta", "Gamma"]),
        ("subject:desc", ["Gamma", "Beta", "Alpha"]),
        ("subject", ["Alpha", "Beta", "Gamma"]),
        (" SUBJECT : DESC ", ["Gamma", "Beta", "Alpha"]),
        ("subject:sideways", ["Alpha", "Beta", "Gamma"]),
        ("salary:desc", ["Gamma", "Beta", "Alpha"]),
        ("salary:desc,subject:desc", ["Gamma", "Beta", "Alpha"]),
        ("priority_id:desc,subject:asc", ["Beta", "Gamma", "Alpha"]),
        # Ties on the chosen key keep the id order.
        ("tracker_id:asc", ["Alpha", "Gamma", "Beta"]),
        ("created_at:asc", ["Alpha", "Beta", "Gamma"]),
        ("updated_at:desc", ["Gamma", "Beta", "Alpha"]),
    ]
    for sort, expected in cases:
        assert await _subjects(db_session, w, every, sort) == (expected, 3), sort
    assert await _subjects(db_session, w, every, "subject:asc", offset=1, limit=1) == (["Beta"], 3)
    assert await _subjects(db_session, w, every, "subject:asc", offset=5, limit=1) == ([], 3)
