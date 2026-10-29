"""Version lists use natural name order and put open/locked versions first.

The data mirrors a real project: nine closed releases from 0.1.8 to 0.10.0 and
the open next ones, some with a due date and some without. Plain text order
would put "0.10.0" before "0.2.0" and list every dated version before every
undated one.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.project import Project
from specivo.models.version import Version
from specivo.services.version_service import VersionService
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_svc = VersionService()
_D = datetime.date

# (name, status, due date) — inserted in a deliberately scrambled order.
_VERSIONS: list[tuple[str, str, datetime.date | None]] = [
    ("0.10.0", "closed", _D(2026, 10, 1)),
    ("0.2.0", "closed", _D(2026, 5, 20)),
    ("0.11.0", "open", None),
    ("0.1.9", "closed", None),
    ("0.9.0", "closed", None),
    ("0.1.10", "closed", _D(2026, 5, 2)),
    ("0.7.0", "closed", _D(2026, 8, 1)),
    ("0.12.0", "open", _D(2026, 12, 1)),
    ("0.3.0", "closed", None),
    ("0.8.0", "closed", _D(2026, 8, 20)),
    ("0.1.8", "closed", _D(2026, 4, 10)),
]

_CLOSED_BY_DUE = ["0.1.8", "0.1.10", "0.2.0", "0.7.0", "0.8.0", "0.10.0", "0.1.9", "0.3.0", "0.9.0"]
_CLOSED_NATURAL = ["0.1.8", "0.1.9", "0.1.10", "0.2.0", "0.3.0", "0.7.0", "0.8.0", "0.9.0", "0.10.0"]


async def _project(db: AsyncSession, key: str, name: str | None = None) -> Project:
    project = ProjectFactory.build(key=key, identifier=key.lower(), path=key.lower(), name=name or key)
    db.add(project)
    await db.flush()
    for version_name, status, due in _VERSIONS:
        db.add(Version(project_id=project.id, name=version_name, status=status, effective_date=due))
    await db.commit()
    return project


async def test_list_for_project_default_order(db_session: AsyncSession):
    project = await _project(db_session, "VORDA")
    names = [v.name for v in await _svc.list_for_project(db_session, project.id)]
    # Open versions by due date (undated last), then closed ones the same way.
    assert names == ["0.12.0", "0.11.0", *_CLOSED_BY_DUE]


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("due:asc", ["0.12.0", "0.11.0", *_CLOSED_BY_DUE]),
        ("version:asc", ["0.11.0", "0.12.0", *_CLOSED_NATURAL]),
        ("version:desc", ["0.12.0", "0.11.0", *reversed(_CLOSED_NATURAL)]),
        (
            "due:desc",
            ["0.12.0", "0.11.0", "0.10.0", "0.8.0", "0.7.0", "0.2.0", "0.1.10", "0.1.8", "0.1.9", "0.3.0", "0.9.0"],
        ),
        ("status:asc", ["0.12.0", "0.11.0", *_CLOSED_BY_DUE]),
        ("status:desc", [*_CLOSED_BY_DUE, "0.12.0", "0.11.0"]),
    ],
)
async def test_roadmap_sort_options(db_session: AsyncSession, sort: str, expected: list[str]):
    project = await _project(db_session, "VORDB")
    admin = AdminUserFactory.build(login="vordb_admin", status="active")
    db_session.add(admin)
    await db_session.flush()
    entries = await _svc.roadmap(db_session, project, admin, sort)
    assert [e.version.name for e in entries] == expected


async def test_list_all_orders_each_project_naturally(db_session: AsyncSession):
    """The admin list keeps projects grouped by name and orders each one's versions."""
    second = await _project(db_session, "VORDD", name="Zulu ordering project")
    first = await _project(db_session, "VORDC", name="Alpha ordering project")

    rows = [r for r in await _svc.list_all(db_session) if r["project_key"] in {first.key, second.key}]
    expected = ["0.12.0", "0.11.0", *_CLOSED_BY_DUE]
    assert [(r["project_key"], r["name"]) for r in rows] == [(first.key, n) for n in expected] + [
        (second.key, n) for n in expected
    ]


async def test_search_for_project_picker_order(db_session: AsyncSession):
    """The picker puts assignable versions first, then closed ones most recent first."""
    project = await _project(db_session, "VORDE")
    names = [v.name for v in await _svc.search_for_project(db_session, project.id)]
    assert names == ["0.12.0", "0.11.0", "0.10.0", "0.8.0", "0.7.0", "0.2.0", "0.1.10", "0.1.8", "0.9.0", "0.3.0"]

    matched = [v.name for v in await _svc.search_for_project(db_session, project.id, "0.1")]
    assert matched == ["0.12.0", "0.11.0", "0.10.0", "0.1.10", "0.1.8", "0.1.9"]
