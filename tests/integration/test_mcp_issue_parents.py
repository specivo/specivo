"""Setting and changing an issue's parent over MCP, and showing parent and children.

``specivo_create_issue`` and ``specivo_update_issue`` take a ``parent_ref``
(a display key, never a database id). Every refusal is an error string and
leaves the data unchanged: an unknown ref, a parent the caller may not see
(with the same message, so nothing leaks), a cycle and the depth limit.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import MAX_HIERARCHY_DEPTH
from specivo.mcp import server
from specivo.mcp.auth import mcp_raw_key_var
from specivo.mcp.tools import _create_issue, _list_issues, _show_issue, _update_issue
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory
from tests.integration.test_api_key_project_scope import build_world

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession):
    status = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    priority = PriorityFactory.build(name="Normal", is_default=True, position=2)
    t = TrackerFactory.build(name="Task", default_status_id=status.id)
    db_session.add_all([priority, t])
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="MPAR", name="MCP Parents", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="mpar_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _issue(
    db: AsyncSession,
    project: Project,
    tracker,
    user: User,
    subject: str,
    parent: Issue | None = None,
    *,
    is_private: bool = False,
):
    issue = await IssueService().create(
        db,
        project,
        IssueCreate(
            project_key=project.key,
            tracker_id=tracker.id,
            subject=subject,
            parent_id=parent.id if parent is not None else None,
            is_private=is_private,
        ),
        user,
    )
    await db.commit()
    await db.refresh(issue)
    return issue


async def _member(db: AsyncSession, project: Project, login: str) -> User:
    """A project member who may view, add and edit issues, but sees no one else's private issue."""
    user = UserFactory.build(login=login, status="active")
    role = Role(name=f"mpar-{login}", permissions=["view_issues", "add_issues", "edit_issues"])
    db.add_all([user, role])
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()
    return user


async def _by_ref(db: AsyncSession, ref: str) -> Issue:
    key, seq = ref.rsplit("-", 1)
    stmt = select(Issue).where(Issue.project_key == key, Issue.sequence_number == int(seq))
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def _assert_nested_set_valid(db: AsyncSession, project_id: int) -> None:
    """Every tree in the project is a gap-free nested set that agrees with ``parent_id``."""
    stmt = select(Issue).where(Issue.project_id == project_id).execution_options(populate_existing=True)
    issues = (await db.execute(stmt)).scalars().all()
    by_id = {i.id: i for i in issues}
    trees: dict[int, list[Issue]] = {}
    for issue in issues:
        trees.setdefault(issue.root_id, []).append(issue)
        if issue.parent_id is None:
            assert issue.root_id == issue.id, issue.display_key
        else:
            parent = by_id[issue.parent_id]
            assert issue.root_id == parent.root_id, issue.display_key
            assert parent.lft < issue.lft < issue.rgt < parent.rgt, issue.display_key
    for members in trees.values():
        bounds = sorted([i.lft for i in members] + [i.rgt for i in members])
        assert bounds == list(range(1, 2 * len(members) + 1))


def _created_ref(result: str) -> str:
    assert result.startswith("Created issue "), result
    return result.split()[2].rstrip(":")


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_with_parent_ref_sets_the_parent(db_session, project, tracker, admin) -> None:
    parent = await _issue(db_session, project, tracker, admin, "Epic")
    result = await _create_issue(db_session, admin, project.key, tracker.id, "Child", parent_ref=parent.display_key)
    child = await _by_ref(db_session, _created_ref(result))
    assert child.parent_id == parent.id
    assert f"Parent: {parent.display_key}" in result
    await _assert_nested_set_valid(db_session, project.id)


async def test_create_parent_ref_is_case_insensitive(db_session, project, tracker, admin) -> None:
    parent = await _issue(db_session, project, tracker, admin, "Epic")
    result = await _create_issue(
        db_session, admin, project.key, tracker.id, "Child", parent_ref=parent.display_key.lower()
    )
    assert (await _by_ref(db_session, _created_ref(result))).parent_id == parent.id


async def test_create_with_unknown_parent_ref_creates_nothing(db_session, project, tracker, admin) -> None:
    before = await db_session.scalar(select(func.count()).select_from(Issue).where(Issue.project_id == project.id))
    result = await _create_issue(db_session, admin, project.key, tracker.id, "Orphan", parent_ref="MPAR-999")
    assert result == "Error: parent issue 'MPAR-999' not found"
    after = await db_session.scalar(select(func.count()).select_from(Issue).where(Issue.project_id == project.id))
    assert after == before


async def test_create_refuses_a_numeric_parent_ref(db_session, project, tracker, admin) -> None:
    """Database ids are never accepted where a display key is expected."""
    parent = await _issue(db_session, project, tracker, admin, "Epic")
    result = await _create_issue(db_session, admin, project.key, tracker.id, "Child", parent_ref=str(parent.id))
    assert result == f"Error: parent issue '{parent.id}' not found"


async def test_create_below_the_depth_limit_is_refused(db_session, project, tracker, admin) -> None:
    deepest = await _issue(db_session, project, tracker, admin, "Level 1")
    for level in range(2, MAX_HIERARCHY_DEPTH + 1):
        deepest = await _issue(db_session, project, tracker, admin, f"Level {level}", parent=deepest)
    result = await _create_issue(db_session, admin, project.key, tracker.id, "Too deep", parent_ref=deepest.display_key)
    assert result.startswith("Error: ") and "depth" in result
    assert await db_session.scalar(select(func.count()).where(Issue.subject == "Too deep")) == 0
    sequence = await db_session.scalar(select(Project.issue_sequence).where(Project.id == project.id))
    assert sequence == MAX_HIERARCHY_DEPTH


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def test_update_re_parents(db_session, project, tracker, admin) -> None:
    first = await _issue(db_session, project, tracker, admin, "First epic")
    second = await _issue(db_session, project, tracker, admin, "Second epic")
    child = await _issue(db_session, project, tracker, admin, "Child", parent=first)
    result = await _update_issue(db_session, admin, child.display_key, parent_ref=second.display_key)
    assert result.startswith("Updated issue"), result
    assert (await _by_ref(db_session, child.display_key)).parent_id == second.id
    await _assert_nested_set_valid(db_session, project.id)


async def test_update_with_empty_parent_ref_moves_to_root(db_session, project, tracker, admin) -> None:
    epic = await _issue(db_session, project, tracker, admin, "Epic")
    child = await _issue(db_session, project, tracker, admin, "Child", parent=epic)
    result = await _update_issue(db_session, admin, child.display_key, parent_ref="")
    assert result.startswith("Updated issue"), result
    moved = await _by_ref(db_session, child.display_key)
    assert moved.parent_id is None and moved.root_id == moved.id
    await _assert_nested_set_valid(db_session, project.id)


async def test_update_without_parent_ref_keeps_the_parent(db_session, project, tracker, admin) -> None:
    epic = await _issue(db_session, project, tracker, admin, "Epic")
    child = await _issue(db_session, project, tracker, admin, "Child", parent=epic)
    result = await _update_issue(db_session, admin, child.display_key, subject="Renamed child", parent_ref=None)
    assert result.startswith("Updated issue"), result
    updated = await _by_ref(db_session, child.display_key)
    assert updated.subject == "Renamed child"
    assert updated.parent_id == epic.id


async def test_update_with_unknown_parent_ref_changes_nothing(db_session, project, tracker, admin) -> None:
    child = await _issue(db_session, project, tracker, admin, "Child")
    result = await _update_issue(db_session, admin, child.display_key, subject="Changed", parent_ref="MPAR-999")
    assert result == "Error: parent issue 'MPAR-999' not found"
    unchanged = await _by_ref(db_session, child.display_key)
    assert unchanged.subject == "Child" and unchanged.parent_id is None


async def test_update_making_an_issue_its_own_ancestor_is_a_cycle(db_session, project, tracker, admin) -> None:
    top = await _issue(db_session, project, tracker, admin, "Top")
    middle = await _issue(db_session, project, tracker, admin, "Middle", parent=top)
    bottom = await _issue(db_session, project, tracker, admin, "Bottom", parent=middle)

    result = await _update_issue(db_session, admin, top.display_key, subject="Changed", parent_ref=bottom.display_key)
    assert result.startswith("Error: ") and "cycle" in result
    unchanged = await _by_ref(db_session, top.display_key)
    assert unchanged.subject == "Top" and unchanged.parent_id is None

    itself = await _update_issue(db_session, admin, top.display_key, parent_ref=top.display_key)
    assert itself.startswith("Error: ") and "own parent" in itself
    await _assert_nested_set_valid(db_session, project.id)


async def test_update_past_the_depth_limit_is_refused(db_session, project, tracker, admin) -> None:
    deepest = await _issue(db_session, project, tracker, admin, "Level 1")
    for level in range(2, MAX_HIERARCHY_DEPTH + 1):
        deepest = await _issue(db_session, project, tracker, admin, f"Level {level}", parent=deepest)
    loose = await _issue(db_session, project, tracker, admin, "Loose")
    result = await _update_issue(db_session, admin, loose.display_key, parent_ref=deepest.display_key)
    assert result.startswith("Error: ") and "depth" in result
    assert (await _by_ref(db_session, loose.display_key)).parent_id is None
    await _assert_nested_set_valid(db_session, project.id)


async def test_update_moving_a_subtree_past_the_depth_limit_is_refused(db_session, project, tracker, admin) -> None:
    """The moved issue fits, but its own child would land below the depth limit."""
    deepest = await _issue(db_session, project, tracker, admin, "Level 1")
    for level in range(2, MAX_HIERARCHY_DEPTH):
        deepest = await _issue(db_session, project, tracker, admin, f"Level {level}", parent=deepest)
    loose = await _issue(db_session, project, tracker, admin, "Loose")
    await _issue(db_session, project, tracker, admin, "Loose child", parent=loose)
    result = await _update_issue(db_session, admin, loose.display_key, parent_ref=deepest.display_key)
    assert result.startswith("Error: ") and "depth" in result
    assert (await _by_ref(db_session, loose.display_key)).parent_id is None
    await _assert_nested_set_valid(db_session, project.id)


# ---------------------------------------------------------------------------
# Show and list
# ---------------------------------------------------------------------------


async def test_show_issue_lists_the_parent_and_the_children(db_session, project, tracker, admin) -> None:
    epic = await _issue(db_session, project, tracker, admin, "Epic")
    child = await _issue(db_session, project, tracker, admin, "Child", parent=epic)
    other = await _issue(db_session, project, tracker, admin, "Other child", parent=epic)

    shown = await _show_issue(db_session, admin, child.display_key, metadata_only=True)
    lines = shown.splitlines()
    assert f"Parent: {epic.display_key} Epic" in lines
    # Next to the other issue fields, before the counts and linked commits.
    assert lines.index(f"Parent: {epic.display_key} Epic") < lines.index("Comments: 0")
    assert not any(line.startswith("Children:") for line in lines)

    parent_view = await _show_issue(db_session, admin, epic.display_key, metadata_only=True)
    assert f"Children: {child.display_key}, {other.display_key}" in parent_view.splitlines()
    assert not any(line.startswith("Parent:") for line in parent_view.splitlines())


async def test_list_issues_filters_by_parent_ref(db_session, project, tracker, admin) -> None:
    epic = await _issue(db_session, project, tracker, admin, "Epic")
    child = await _issue(db_session, project, tracker, admin, "Child", parent=epic)
    grandchild = await _issue(db_session, project, tracker, admin, "Grandchild", parent=child)
    loose = await _issue(db_session, project, tracker, admin, "Loose")

    listed = await _list_issues(db_session, admin, project.key, status="all", parent_ref=epic.display_key)
    assert child.display_key in listed
    assert grandchild.display_key not in listed and loose.display_key not in listed
    assert f"parent={epic.display_key}" in listed

    unknown = await _list_issues(db_session, admin, project.key, status="all", parent_ref="MPAR-999")
    assert unknown == "Error: parent issue 'MPAR-999' not found"


# ---------------------------------------------------------------------------
# Private issues
# ---------------------------------------------------------------------------


async def test_show_issue_hides_private_relatives(db_session, project, tracker, admin) -> None:
    epic = await _issue(db_session, project, tracker, admin, "Secret epic", is_private=True)
    child = await _issue(db_session, project, tracker, admin, "Open child", parent=epic)
    hidden = await _issue(db_session, project, tracker, admin, "Secret grandchild", parent=child, is_private=True)
    shown_child = await _issue(db_session, project, tracker, admin, "Open grandchild", parent=child)
    member = await _member(db_session, project, "mpar_member")

    shown = await _show_issue(db_session, member, child.display_key, metadata_only=True)
    lines = shown.splitlines()
    assert not any(line.startswith("Parent:") for line in lines)
    assert "Secret" not in shown and epic.display_key not in shown and hidden.display_key not in shown
    assert f"Children: {shown_child.display_key}" in lines


async def test_a_private_parent_reads_as_unknown(db_session, project, tracker, admin) -> None:
    epic = await _issue(db_session, project, tracker, admin, "Secret epic", is_private=True)
    loose = await _issue(db_session, project, tracker, admin, "Loose")
    member = await _member(db_session, project, "mpar_member")
    expected = f"Error: parent issue '{epic.display_key}' not found"

    created = await _create_issue(db_session, member, project.key, tracker.id, "Sneaky", parent_ref=epic.display_key)
    assert created == expected
    assert await _update_issue(db_session, member, loose.display_key, parent_ref=epic.display_key) == expected
    assert await _list_issues(db_session, member, project.key, status="all", parent_ref=epic.display_key) == expected
    assert await db_session.scalar(select(func.count()).where(Issue.subject == "Sneaky")) == 0
    assert (await _by_ref(db_session, loose.display_key)).parent_id is None


# ---------------------------------------------------------------------------
# API key project limit
# ---------------------------------------------------------------------------


@pytest.fixture
def use_test_session(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run every tool call on the test's rolled-back session."""

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(server, "get_session_factory", lambda: _session)
    token = mcp_raw_key_var.set(None)
    yield
    mcp_raw_key_var.reset(token)


async def test_a_parent_outside_the_key_limit_reads_as_unknown(
    use_test_session, client: AsyncClient, db_session: AsyncSession
) -> None:
    w = await build_world(db_session, client, "KMP")
    client.headers.pop("Authorization", None)
    a_key, a_ref, b_ref = w.a.key, w.a_issue["key"], w.b_issue["key"]
    mcp_raw_key_var.set(w.limited_raw)

    missing = f"{w.b.key}-999"
    for ref in (b_ref, missing):
        created = await server.specivo_create_issue(
            project_key=a_key, tracker_id=w.tracker_id, subject="KMP child", parent_ref=ref
        )
        assert created == f"Error: parent issue '{ref}' not found"
        updated = await server.specivo_update_issue(issue_ref=a_ref, parent_ref=ref)
        assert updated == f"Error: parent issue '{ref}' not found"
        listed = await server.specivo_list_issues(project_key=a_key, parent_ref=ref)
        assert listed == f"Error: parent issue '{ref}' not found"
    assert await db_session.scalar(select(func.count()).where(Issue.subject == "KMP child")) == 0


async def test_show_issue_hides_relatives_outside_the_key_limit(
    use_test_session, client: AsyncClient, db_session: AsyncSession
) -> None:
    w = await build_world(db_session, client, "KMS")
    client.headers.pop("Authorization", None)
    a_ref, b_ref = w.a_issue["key"], w.b_issue["key"]
    mcp_raw_key_var.set(w.open_raw)
    assert (await server.specivo_update_issue(issue_ref=b_ref, parent_ref=a_ref)).startswith("Updated issue")
    a_child = _created_ref(
        await server.specivo_create_issue(
            project_key=w.a.key, tracker_id=w.tracker_id, subject="KMS a child", parent_ref=a_ref
        )
    )
    assert f"Children: {b_ref}, {a_child}" in await server.specivo_show_issue(issue_ref=a_ref)

    mcp_raw_key_var.set(w.limited_raw)
    shown = await server.specivo_show_issue(issue_ref=a_ref)
    assert f"Children: {a_child}" in shown.splitlines()
    assert b_ref not in shown
