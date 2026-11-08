"""``specivo_list_issues`` narrowed by assignee, tag and release, like the issue list's filters.

Each argument takes a name or a numeric id; ``assignee`` also ``me`` and
``none``, ``release`` also ``none``. Only what the issue list's own pickers
offer resolves: people issues of the project can be assigned to, the
project's tags and the versions visible from the project. Anything else is an
error naming the argument, the same whether it does not exist or is out of
sight, and never an empty list.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp.tools import _list_issues
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.tag import Tag, TagLink
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory
from tests.factories.version import VersionFactory

pytestmark = pytest.mark.asyncio(loop_scope="function")


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> dict:
    db = db_session
    status = StatusFactory.build(name="New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="Task", default_status_id=status.id)
    db.add_all([tracker, PriorityFactory.build(name="Normal", is_default=True, position=2)])
    project = ProjectFactory.build(key="MLF", name="MCP list filters", is_public=False)
    other = ProjectFactory.build(key="MLFO", name="Other", is_public=False)
    admin = AdminUserFactory.build(login="mlf_admin", status="active")
    db.add_all([project, other, admin])
    await db.flush()

    role = Role(name="mlf-dev", permissions=["view_issues", "add_issues", "edit_issues"])
    db.add(role)
    await db.flush()

    async def member(login: str, proj: Project) -> User:
        user = UserFactory.build(login=login, display_name=f"Somchai {login}", status="active")
        db.add(user)
        await db.flush()
        m = Member(user_id=user.id, project_id=proj.id)
        db.add(m)
        await db.flush()
        db.add(MemberRole(member_id=m.id, role_id=role.id))
        await db.flush()
        return user

    dev = await member("mlf_dev", project)
    tester = await member("mlf_tester", project)
    outsider = await member("mlf_outsider", other)

    release = VersionFactory.build(project_id=project.id, name="1.4")
    shared = VersionFactory.build(project_id=other.id, name="Platform 2", sharing="system")
    hidden = VersionFactory.build(project_id=other.id, name="Other only", sharing="none")
    frontend = Tag(project_id=project.id, name="Frontend")
    other_tag = Tag(project_id=other.id, name="Backend")
    db.add_all([release, shared, hidden, frontend, other_tag])
    await db.flush()

    svc = IssueService()

    async def issue(subject: str, **fields) -> Issue:
        created = await svc.create(
            db, project, IssueCreate(project_key=project.key, tracker_id=tracker.id, subject=subject, **fields), admin
        )
        await db.flush()
        return created

    issues = {
        "dev_release": await issue("Dev on 1.4", assigned_to_id=dev.id, fixed_version_id=release.id),
        "tester": await issue("Tester, no release", assigned_to_id=tester.id),
        "shared": await issue("Unassigned on platform"),
        "loose": await issue("Unassigned, no release"),
    }
    # Issue creation takes the project's own versions only; a shared one is set directly.
    issues["shared"].fixed_version_id = shared.id
    db.add(TagLink(tag_id=frontend.id, issue_id=issues["tester"].id))
    await db.commit()
    return {
        "project": project,
        "admin": admin,
        "dev": dev,
        "outsider": outsider,
        "release": release,
        "shared": shared,
        "hidden": hidden,
        "frontend": frontend,
        "other_tag": other_tag,
        "issues": issues,
    }


def _keys(listing: str, world: dict) -> set[str]:
    return {name for name, issue in world["issues"].items() if f"  {issue.display_key}  " in listing}


async def _list(db: AsyncSession, world: dict, user: User | None = None, **kw) -> str:
    return await _list_issues(db, user or world["admin"], world["project"].key, status="all", **kw)


async def test_assignee_by_login_id_me_and_none(db_session, world) -> None:
    dev = world["dev"]
    by_login = await _list(db_session, world, assignee="MLF_DEV")
    assert _keys(by_login, world) == {"dev_release"}
    assert "assignee=MLF_DEV" in by_login
    assert _keys(await _list(db_session, world, assignee=str(dev.id)), world) == {"dev_release"}
    assert _keys(await _list(db_session, world, dev, assignee="me"), world) == {"dev_release"}
    assert _keys(await _list(db_session, world, assignee="none"), world) == {"shared", "loose"}


async def test_tag_by_name_and_id(db_session, world) -> None:
    assert _keys(await _list(db_session, world, tag="frontend"), world) == {"tester"}
    assert _keys(await _list(db_session, world, tag=str(world["frontend"].id)), world) == {"tester"}


async def test_release_by_name_id_shared_and_none(db_session, world) -> None:
    assert _keys(await _list(db_session, world, release="1.4"), world) == {"dev_release"}
    assert _keys(await _list(db_session, world, release=str(world["release"].id)), world) == {"dev_release"}
    assert _keys(await _list(db_session, world, release="platform 2"), world) == {"shared"}
    assert _keys(await _list(db_session, world, release="none"), world) == {"tester", "loose"}


async def test_filters_combine(db_session, world) -> None:
    assert _keys(await _list(db_session, world, assignee="none", release="none"), world) == {"loose"}
    assert _keys(await _list(db_session, world, assignee="mlf_dev", tag="frontend"), world) == set()


@pytest.mark.parametrize(
    ("argument", "value", "message"),
    [
        ("assignee", "nobody_here", "Error: Unknown assignee 'nobody_here' in MLF"),
        # A real person, but not someone issues of this project can be assigned to.
        ("assignee", "mlf_outsider", "Error: Unknown assignee 'mlf_outsider' in MLF"),
        ("tag", "Missing", "Error: Unknown tag 'Missing' in MLF."),
        # Another project's tag reads exactly like a missing one.
        ("tag", "Backend", "Error: Unknown tag 'Backend' in MLF."),
        ("release", "9.9", "Error: Unknown release '9.9' in MLF"),
        # A version of another project that is not shared reads like a missing one.
        ("release", "Other only", "Error: Unknown release 'Other only' in MLF"),
    ],
)
async def test_unknown_or_hidden_values_are_errors(db_session, world, argument, value, message) -> None:
    result = await _list(db_session, world, **{argument: value})
    assert result.startswith(message)
    assert "Issues for" not in result


async def test_ids_out_of_sight_read_like_unknown_ones(db_session, world) -> None:
    for argument, value in (
        ("assignee", str(world["outsider"].id)),
        ("tag", str(world["other_tag"].id)),
        ("release", str(world["hidden"].id)),
    ):
        result = await _list(db_session, world, **{argument: value})
        assert result.startswith(f"Error: Unknown {argument} '{value}' in MLF"), result


async def test_blank_arguments_do_not_filter(db_session, world) -> None:
    assert _keys(await _list(db_session, world, assignee=" ", tag="", release=None), world) == set(world["issues"])
