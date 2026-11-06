"""The hierarchy depth limit counts the moved issue's own sub-tasks.

When an issue gets a new parent its whole subtree moves along, so the
deepest descendant, not just the issue, has to stay within
``MAX_HIERARCHY_DEPTH`` levels. Covered here for the service, the REST API
and the import's parent links (the MCP tools are in
``test_mcp_issue_parents.py``, which is serial).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import MAX_HIERARCHY_DEPTH
from specivo.core.exceptions import ValidationError
from specivo.core.i18n import activate, deactivate
from specivo.importers.load.issue_links import PARENT_REFUSED, set_parent
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate, IssueUpdate
from specivo.services.issue_service import IssueService
from specivo.services.nested_set_service import NestedSetService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_service = IssueService()
_nested = NestedSetService()


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession):
    status = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    priority = PriorityFactory.build(name="Normal", is_default=True, position=2)
    t = TrackerFactory.build(name="Task", default_status_id=status.id)
    db_session.add_all([priority, t])
    await db_session.commit()
    return t


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="PDST", name="Parent depth", is_public=False)
    db_session.add(proj)
    await db_session.commit()
    return proj


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="pdst_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


async def _issue(db: AsyncSession, project: Project, tracker, user: User, subject: str, parent: Issue | None = None):
    issue = await _service.create(
        db,
        project,
        IssueCreate(
            project_key=project.key,
            tracker_id=tracker.id,
            subject=subject,
            parent_id=parent.id if parent is not None else None,
        ),
        user,
    )
    await db.commit()
    await db.refresh(issue)
    return issue


async def _chain(db, project, tracker, user, levels: int, name: str) -> list[Issue]:
    """*levels* issues, each the child of the one before; the first is a root."""
    issues = [await _issue(db, project, tracker, user, f"{name} 1")]
    for level in range(2, levels + 1):
        issues.append(await _issue(db, project, tracker, user, f"{name} {level}", parent=issues[-1]))
    return issues


async def _fresh(db: AsyncSession, issue: Issue) -> Issue:
    stmt = select(Issue).where(Issue.id == issue.id).execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one()


async def _assert_nested_set_valid(db: AsyncSession, project_id: int) -> None:
    """Every tree in the project is a gap-free nested set that agrees with ``parent_id``."""
    stmt = select(Issue).where(Issue.project_id == project_id).execution_options(populate_existing=True)
    issues = (await db.execute(stmt)).scalars().all()
    by_id = {i.id: i for i in issues}
    trees: dict[int, list[Issue]] = {}
    for issue in issues:
        trees.setdefault(issue.root_id, []).append(issue)
        if issue.parent_id is not None:
            parent = by_id[issue.parent_id]
            assert parent.lft < issue.lft < issue.rgt < parent.rgt, issue.display_key
    for members in trees.values():
        bounds = sorted([i.lft for i in members] + [i.rgt for i in members])
        assert bounds == list(range(1, 2 * len(members) + 1))


class TestSubtreeHeight:
    async def test_leaf_has_no_height(self, db_session, project, tracker, admin):
        (leaf,) = await _chain(db_session, project, tracker, admin, 1, "Leaf")
        assert await _nested.subtree_height(db_session, leaf) == 0

    async def test_height_is_the_deepest_branch(self, db_session, project, tracker, admin):
        top, middle, _bottom = await _chain(db_session, project, tracker, admin, 3, "Branch")
        await _issue(db_session, project, tracker, admin, "Short branch", parent=top)
        assert await _nested.subtree_height(db_session, await _fresh(db_session, top)) == 2
        assert await _nested.subtree_height(db_session, await _fresh(db_session, middle)) == 1

    async def test_one_query_however_deep(self, db_session, project, tracker, admin):
        chain = await _chain(db_session, project, tracker, admin, 6, "Deep")
        target = await _issue(db_session, project, tracker, admin, "Target")
        statements: list[str] = []

        def _count(_conn, _cursor, statement, *_args):
            statements.append(statement)

        engine = db_session.bind.sync_engine if hasattr(db_session.bind, "sync_engine") else db_session.bind
        event.listen(engine, "before_cursor_execute", _count)
        try:
            await _nested.validate_parent(db_session, chain[0], target)
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        assert len(statements) == 1, statements


class TestService:
    async def test_moving_a_subtree_below_the_limit_is_refused(self, db_session, project, tracker, admin):
        """The moved issue alone would fit; its sub-tasks would not."""
        parents = await _chain(db_session, project, tracker, admin, MAX_HIERARCHY_DEPTH - 3, "Parent")
        moved = await _chain(db_session, project, tracker, admin, 4, "Moved")

        with pytest.raises(ValidationError) as exc:
            await _service.update(
                db_session,
                moved[0],
                IssueUpdate(parent_id=parents[-1].id, lock_version=moved[0].lock_version),
                admin,
            )
        assert exc.value.field == "parent_id"
        assert exc.value.details == {"max_depth": MAX_HIERARCHY_DEPTH, "current_depth": MAX_HIERARCHY_DEPTH}
        assert "depth" in exc.value.message.lower()
        assert (await _fresh(db_session, moved[0])).parent_id is None
        await _assert_nested_set_valid(db_session, project.id)

    async def test_moving_a_subtree_exactly_to_the_limit_is_allowed(self, db_session, project, tracker, admin):
        parents = await _chain(db_session, project, tracker, admin, MAX_HIERARCHY_DEPTH - 3, "Parent")
        moved = await _chain(db_session, project, tracker, admin, 3, "Moved")

        await _service.update(
            db_session,
            moved[0],
            IssueUpdate(parent_id=parents[-1].id, lock_version=moved[0].lock_version),
            admin,
        )
        await db_session.commit()
        assert (await _fresh(db_session, moved[0])).parent_id == parents[-1].id
        assert len(await _nested.get_ancestors(db_session, await _fresh(db_session, moved[-1]))) == (
            MAX_HIERARCHY_DEPTH - 1
        )
        await _assert_nested_set_valid(db_session, project.id)

    async def test_only_the_deepest_branch_counts(self, db_session, project, tracker, admin):
        """A wide but shallow subtree fits where a deep one does not."""
        parents = await _chain(db_session, project, tracker, admin, MAX_HIERARCHY_DEPTH - 2, "Parent")
        moved = await _issue(db_session, project, tracker, admin, "Wide")
        for n in range(3):
            await _issue(db_session, project, tracker, admin, f"Wide child {n}", parent=moved)
        moved = await _fresh(db_session, moved)

        await _service.update(
            db_session, moved, IssueUpdate(parent_id=parents[-1].id, lock_version=moved.lock_version), admin
        )
        await db_session.commit()
        assert (await _fresh(db_session, moved)).parent_id == parents[-1].id
        await _assert_nested_set_valid(db_session, project.id)

    async def test_moving_within_the_same_tree_counts_the_subtree(self, db_session, project, tracker, admin):
        chain = await _chain(db_session, project, tracker, admin, MAX_HIERARCHY_DEPTH - 2, "Same")
        sibling = await _issue(db_session, project, tracker, admin, "Sibling", parent=chain[0])
        child = await _issue(db_session, project, tracker, admin, "Sibling child", parent=sibling)
        await _issue(db_session, project, tracker, admin, "Sibling grandchild", parent=child)
        sibling = await _fresh(db_session, sibling)
        # Under the chain's deepest issue, the sibling's grandchild would be one level too deep.
        with pytest.raises(ValidationError):
            await _nested.validate_parent(db_session, sibling, await _fresh(db_session, chain[-1]))
        # One level higher it fits.
        await _nested.validate_parent(db_session, sibling, await _fresh(db_session, chain[-2]))

    async def test_the_message_is_translated(self, db_session, project, tracker, admin):
        parents = await _chain(db_session, project, tracker, admin, MAX_HIERARCHY_DEPTH - 1, "Parent")
        moved = await _chain(db_session, project, tracker, admin, 2, "Moved")
        activate("th")
        try:
            with pytest.raises(ValidationError) as exc:
                await _nested.validate_parent(db_session, moved[0], parents[-1])
        finally:
            deactivate()
        assert "Hierarchy depth" not in exc.value.message
        assert str(MAX_HIERARCHY_DEPTH) in exc.value.message
        # Ten levels allowed; the moved issue's child would be on the eleventh.
        assert str(MAX_HIERARCHY_DEPTH + 1) in exc.value.message


class TestRestApi:
    async def test_patch_parent_counts_the_subtree(self, admin_client: AsyncClient, db_session, tracker):
        project = ProjectFactory.build(key="PDSR", name="Parent depth REST", is_public=False)
        db_session.add(project)
        await db_session.commit()
        user = admin_client.state.user
        parents = await _chain(db_session, project, tracker, user, MAX_HIERARCHY_DEPTH - 2, "Parent")
        moved = await _chain(db_session, project, tracker, user, 3, "Moved")

        resp = await admin_client.patch(
            f"/api/v1/issues/{moved[0].display_key}/",
            json={"parent_id": parents[-1].id, "lock_version": moved[0].lock_version},
        )
        assert resp.status_code == 422, resp.text
        (error,) = resp.json()["errors"]
        assert error["field"] == "parent_id"
        assert "depth" in error["message"].lower()
        assert error["details"] == {"max_depth": MAX_HIERARCHY_DEPTH, "current_depth": MAX_HIERARCHY_DEPTH}
        assert (await _fresh(db_session, moved[0])).parent_id is None
        await _assert_nested_set_valid(db_session, project.id)

        # One level less fits.
        resp = await admin_client.patch(
            f"/api/v1/issues/{moved[0].display_key}/",
            json={"parent_id": parents[-2].id, "lock_version": moved[0].lock_version},
        )
        assert resp.status_code == 200, resp.text
        await _assert_nested_set_valid(db_session, project.id)


class TestImport:
    async def test_a_parent_link_counts_the_subtree(self, db_session, project, tracker, admin):
        parents = await _chain(db_session, project, tracker, admin, MAX_HIERARCHY_DEPTH - 2, "Parent")
        moved = await _chain(db_session, project, tracker, admin, 3, "Moved")

        linked, problem = await set_parent(db_session, "child-ref", "parent-ref", moved[0].id, parents[-1].id)
        assert linked is False
        assert problem is not None and problem.message == PARENT_REFUSED
        assert "depth" in problem.context["reason"].lower()
        assert (await _fresh(db_session, moved[0])).parent_id is None
        await _assert_nested_set_valid(db_session, project.id)

    async def test_a_parent_link_that_fits_is_made(self, db_session, project, tracker, admin):
        parents = await _chain(db_session, project, tracker, admin, MAX_HIERARCHY_DEPTH - 3, "Parent")
        moved = await _chain(db_session, project, tracker, admin, 3, "Moved")

        linked, problem = await set_parent(db_session, "child-ref", "parent-ref", moved[0].id, parents[-1].id)
        assert (linked, problem) == (True, None)
        await _assert_nested_set_valid(db_session, project.id)
