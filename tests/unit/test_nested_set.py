"""Unit tests for NestedSetService.

Tests cover:
- insert_root: sets lft=1, rgt=2, root_id=self.id
- insert_child: correct lft/rgt and boundary adjustments
- get_descendants / get_ancestors: correct node sets
- is_ancestor_of / is_descendant_of: relationship checks
- validate_parent: self-reference, cycle detection, max depth
- recalculate_parent_attributes: done_ratio, start_date, due_date derivation
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import ValidationError
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.nested_set_service import NestedSetService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, status: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Task", default_status_id=status.id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def priority(db_session: AsyncSession) -> IssuePriority:
    p = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="NST", identifier="nested-set-test")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def user(db_session: AsyncSession) -> User:
    u = AdminUserFactory.build(login="ns_admin", status="active")
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


def _make_issue(
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    user: User,
    subject: str,
    seq: int,
) -> object:
    """Build an Issue ORM instance (not saved)."""
    from specivo.models.issue import Issue

    return Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=seq,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject=subject,
        lft=1,
        rgt=2,
    )


# ---------------------------------------------------------------------------
# Helper: create and flush a single root issue
# ---------------------------------------------------------------------------


async def _create_root(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    user: User,
    subject: str,
    seq: int,
) -> object:
    from specivo.models.issue import Issue

    issue = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=seq,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject=subject,
        lft=1,
        rgt=2,
    )
    db_session.add(issue)
    await db_session.flush()
    svc = NestedSetService()
    await svc.insert_root(db_session, issue)
    await db_session.commit()
    await db_session.refresh(issue)
    return issue


# ---------------------------------------------------------------------------
# insert_root
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_root_sets_correct_values(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    user: User,
) -> None:
    """insert_root sets lft=1, rgt=2, root_id=self.id, parent_id=None."""
    from specivo.models.issue import Issue

    issue = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Root issue",
        lft=1,
        rgt=2,
    )
    db_session.add(issue)
    await db_session.flush()

    svc = NestedSetService()
    await svc.insert_root(db_session, issue)
    await db_session.commit()
    await db_session.refresh(issue)

    assert issue.lft == 1
    assert issue.rgt == 2
    assert issue.root_id == issue.id
    assert issue.parent_id is None


# ---------------------------------------------------------------------------
# insert_child
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_child_adjusts_boundaries(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    user: User,
) -> None:
    """insert_child places child at parent.rgt and shifts other nodes."""
    from specivo.models.issue import Issue

    svc = NestedSetService()

    # Create root
    root = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Root",
        lft=1,
        rgt=2,
    )
    db_session.add(root)
    await db_session.flush()
    await svc.insert_root(db_session, root)

    # Create child (flush first to get id)
    child = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=2,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Child",
        lft=1,
        rgt=2,
    )
    db_session.add(child)
    await db_session.flush()
    await svc.insert_child(db_session, root, child)
    await db_session.commit()

    await db_session.refresh(root)
    await db_session.refresh(child)

    # Root should expand: lft=1, rgt=4 (was 2, gained 2)
    assert root.lft == 1
    assert root.rgt == 4
    # Child placed at root's old rgt
    assert child.lft == 2
    assert child.rgt == 3
    assert child.parent_id == root.id
    assert child.root_id == root.id


@pytest.mark.asyncio
async def test_insert_two_children_correct_order(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    user: User,
) -> None:
    """Two children are inserted in order: first child lft < second child lft."""
    from specivo.models.issue import Issue

    svc = NestedSetService()

    root = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Root",
        lft=1,
        rgt=2,
    )
    db_session.add(root)
    await db_session.flush()
    await svc.insert_root(db_session, root)

    child1 = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=2,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Child1",
        lft=1,
        rgt=2,
    )
    db_session.add(child1)
    await db_session.flush()
    await svc.insert_child(db_session, root, child1)

    await db_session.refresh(root)

    child2 = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=3,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Child2",
        lft=1,
        rgt=2,
    )
    db_session.add(child2)
    await db_session.flush()
    await svc.insert_child(db_session, root, child2)
    await db_session.commit()

    await db_session.refresh(root)
    await db_session.refresh(child1)
    await db_session.refresh(child2)

    # root: lft=1, rgt=6
    assert root.lft == 1
    assert root.rgt == 6
    # child1 comes first: lft=2 rgt=3
    assert child1.lft == 2
    assert child1.rgt == 3
    # child2 is rightmost: lft=4 rgt=5
    assert child2.lft == 4
    assert child2.rgt == 5


# ---------------------------------------------------------------------------
# get_descendants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_descendants_returns_all_subtree_nodes(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    user: User,
) -> None:
    """get_descendants returns all nodes in the subtree, excluding self."""
    from specivo.models.issue import Issue

    svc = NestedSetService()

    # Build: root -> child1 -> grandchild, root -> child2
    root = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Root",
        lft=1,
        rgt=2,
    )
    db_session.add(root)
    await db_session.flush()
    await svc.insert_root(db_session, root)

    child1 = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=2,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Child1",
        lft=1,
        rgt=2,
    )
    db_session.add(child1)
    await db_session.flush()
    await svc.insert_child(db_session, root, child1)
    await db_session.refresh(root)

    grandchild = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=3,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Grandchild",
        lft=1,
        rgt=2,
    )
    db_session.add(grandchild)
    await db_session.flush()
    await svc.insert_child(db_session, child1, grandchild)
    await db_session.refresh(root)
    await db_session.refresh(child1)

    child2 = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=4,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Child2",
        lft=1,
        rgt=2,
    )
    db_session.add(child2)
    await db_session.flush()
    await svc.insert_child(db_session, root, child2)
    await db_session.commit()

    await db_session.refresh(root)

    descendants = await svc.get_descendants(db_session, root)
    descendant_ids = {d.id for d in descendants}

    assert child1.id in descendant_ids
    assert grandchild.id in descendant_ids
    assert child2.id in descendant_ids
    assert root.id not in descendant_ids
    assert len(descendants) == 3


# ---------------------------------------------------------------------------
# get_ancestors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ancestors_returns_path_to_root(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    user: User,
) -> None:
    """get_ancestors returns root and all nodes between root and issue."""
    from specivo.models.issue import Issue

    svc = NestedSetService()

    root = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Root",
        lft=1,
        rgt=2,
    )
    db_session.add(root)
    await db_session.flush()
    await svc.insert_root(db_session, root)

    child = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=2,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Child",
        lft=1,
        rgt=2,
    )
    db_session.add(child)
    await db_session.flush()
    await svc.insert_child(db_session, root, child)
    await db_session.refresh(root)

    grandchild = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=3,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Grandchild",
        lft=1,
        rgt=2,
    )
    db_session.add(grandchild)
    await db_session.flush()
    await svc.insert_child(db_session, child, grandchild)
    await db_session.commit()

    await db_session.refresh(grandchild)

    ancestors = await svc.get_ancestors(db_session, grandchild)
    ancestor_ids = [a.id for a in ancestors]

    assert root.id in ancestor_ids
    assert child.id in ancestor_ids
    assert grandchild.id not in ancestor_ids
    # Ordered root-first
    assert ancestor_ids.index(root.id) < ancestor_ids.index(child.id)


# ---------------------------------------------------------------------------
# is_ancestor_of / is_descendant_of
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_ancestor_of_returns_true_for_ancestor(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    user: User,
) -> None:
    from specivo.models.issue import Issue

    svc = NestedSetService()

    root = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Root",
        lft=1,
        rgt=2,
    )
    db_session.add(root)
    await db_session.flush()
    await svc.insert_root(db_session, root)

    child = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=2,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Child",
        lft=1,
        rgt=2,
    )
    db_session.add(child)
    await db_session.flush()
    await svc.insert_child(db_session, root, child)
    await db_session.commit()
    await db_session.refresh(root)
    await db_session.refresh(child)

    assert await svc.is_ancestor_of(db_session, root, child) is True
    assert await svc.is_ancestor_of(db_session, child, root) is False
    assert await svc.is_descendant_of(db_session, child, root) is True
    assert await svc.is_descendant_of(db_session, root, child) is False


@pytest.mark.asyncio
async def test_is_ancestor_of_returns_false_for_sibling(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    user: User,
) -> None:
    from specivo.models.issue import Issue

    svc = NestedSetService()

    root = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Root",
        lft=1,
        rgt=2,
    )
    db_session.add(root)
    await db_session.flush()
    await svc.insert_root(db_session, root)

    child1 = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=2,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Sibling1",
        lft=1,
        rgt=2,
    )
    db_session.add(child1)
    await db_session.flush()
    await svc.insert_child(db_session, root, child1)
    await db_session.refresh(root)

    child2 = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=3,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Sibling2",
        lft=1,
        rgt=2,
    )
    db_session.add(child2)
    await db_session.flush()
    await svc.insert_child(db_session, root, child2)
    await db_session.commit()
    await db_session.refresh(child1)
    await db_session.refresh(child2)

    assert await svc.is_ancestor_of(db_session, child1, child2) is False
    assert await svc.is_ancestor_of(db_session, child2, child1) is False


# ---------------------------------------------------------------------------
# validate_parent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_parent_rejects_self(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    user: User,
) -> None:
    from specivo.models.issue import Issue

    svc = NestedSetService()

    root = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Root",
        lft=1,
        rgt=2,
    )
    db_session.add(root)
    await db_session.flush()
    await svc.insert_root(db_session, root)
    await db_session.commit()
    await db_session.refresh(root)

    with pytest.raises(ValidationError, match="own parent"):
        await svc.validate_parent(db_session, root, root)


@pytest.mark.asyncio
async def test_validate_parent_rejects_cycle(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    user: User,
) -> None:
    """Cannot set a descendant as the parent (A → B: B cannot become parent of A)."""
    from specivo.models.issue import Issue

    svc = NestedSetService()

    root = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Root",
        lft=1,
        rgt=2,
    )
    db_session.add(root)
    await db_session.flush()
    await svc.insert_root(db_session, root)

    child = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=2,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Child",
        lft=1,
        rgt=2,
    )
    db_session.add(child)
    await db_session.flush()
    await svc.insert_child(db_session, root, child)
    await db_session.commit()
    await db_session.refresh(root)
    await db_session.refresh(child)

    # Trying to set child as parent of root creates a cycle
    with pytest.raises(ValidationError, match="[Cc]ycle|descendant"):
        await svc.validate_parent(db_session, root, child)


# ---------------------------------------------------------------------------
# recalculate_parent_attributes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recalculate_parent_done_ratio_weighted_average(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    user: User,
) -> None:
    """done_ratio is weighted by estimated_hours when present."""
    from specivo.models.issue import Issue

    svc = NestedSetService()

    root = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Root",
        lft=1,
        rgt=2,
        done_ratio=0,
    )
    db_session.add(root)
    await db_session.flush()
    await svc.insert_root(db_session, root)

    # child1: 50% done, 10h estimate
    child1 = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=2,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Child1",
        lft=1,
        rgt=2,
        done_ratio=50,
        estimated_hours=Decimal("10"),
    )
    db_session.add(child1)
    await db_session.flush()
    await svc.insert_child(db_session, root, child1)
    await db_session.refresh(root)

    # child2: 100% done, 10h estimate → weighted avg = (50*10 + 100*10) / 20 = 75
    child2 = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=3,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Child2",
        lft=1,
        rgt=2,
        done_ratio=100,
        estimated_hours=Decimal("10"),
    )
    db_session.add(child2)
    await db_session.flush()
    await svc.insert_child(db_session, root, child2)
    await db_session.commit()
    await db_session.refresh(root)

    await svc.recalculate_parent_attributes(db_session, root)
    await db_session.commit()
    await db_session.refresh(root)

    assert root.done_ratio == 75


@pytest.mark.asyncio
async def test_recalculate_parent_done_ratio_equal_weight_no_estimates(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    user: User,
) -> None:
    """Without estimates, done_ratio is a simple average."""
    from specivo.models.issue import Issue

    svc = NestedSetService()

    root = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Root",
        lft=1,
        rgt=2,
        done_ratio=0,
    )
    db_session.add(root)
    await db_session.flush()
    await svc.insert_root(db_session, root)

    child1 = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=2,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Child1",
        lft=1,
        rgt=2,
        done_ratio=0,
        estimated_hours=None,
    )
    db_session.add(child1)
    await db_session.flush()
    await svc.insert_child(db_session, root, child1)
    await db_session.refresh(root)

    child2 = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=3,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Child2",
        lft=1,
        rgt=2,
        done_ratio=100,
        estimated_hours=None,
    )
    db_session.add(child2)
    await db_session.flush()
    await svc.insert_child(db_session, root, child2)
    await db_session.commit()
    await db_session.refresh(root)

    await svc.recalculate_parent_attributes(db_session, root)
    await db_session.commit()
    await db_session.refresh(root)

    # (0 + 100) / 2 = 50
    assert root.done_ratio == 50


@pytest.mark.asyncio
async def test_recalculate_parent_dates(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    user: User,
) -> None:
    """start_date = MIN of children, due_date = MAX of children."""
    from specivo.models.issue import Issue

    svc = NestedSetService()

    root = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Root",
        lft=1,
        rgt=2,
    )
    db_session.add(root)
    await db_session.flush()
    await svc.insert_root(db_session, root)

    child1 = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=2,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Child1",
        lft=1,
        rgt=2,
        start_date=date(2026, 1, 5),
        due_date=date(2026, 1, 20),
    )
    db_session.add(child1)
    await db_session.flush()
    await svc.insert_child(db_session, root, child1)
    await db_session.refresh(root)

    child2 = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=3,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject="Child2",
        lft=1,
        rgt=2,
        start_date=date(2026, 1, 1),
        due_date=date(2026, 2, 1),
    )
    db_session.add(child2)
    await db_session.flush()
    await svc.insert_child(db_session, root, child2)
    await db_session.commit()
    await db_session.refresh(root)

    await svc.recalculate_parent_attributes(db_session, root)
    await db_session.commit()
    await db_session.refresh(root)

    assert root.start_date == date(2026, 1, 1)  # MIN
    assert root.due_date == date(2026, 2, 1)  # MAX
