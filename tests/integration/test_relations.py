"""Integration tests for the 9-type issue relation system.

Tests cover:
- Create relates relation
- Create blocks relation
- Create precedes relation with delay (auto-reschedule)
- Circular dependency blocked (A blocks B, B blocks A → 422)
- Parent-descendant relation blocked → 422
- Duplicate relation blocked → 422
- Delete relation → 204
- List relations shows correct types per direction
- Reverse type normalisation (creating 'follows' stores as 'precedes' swapped)
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"login": login, "password": "testpassword"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _create_issue(
    client: AsyncClient,
    token: str,
    project_key: str,
    tracker_id: int,
    status_id: int,
    priority_id: int,
    subject: str,
    parent_id: int | None = None,
    due_date: str | None = None,
    start_date: str | None = None,
) -> dict:
    body: dict = {
        "project_key": project_key,
        "tracker_id": tracker_id,
        "subject": subject,
        "status_id": status_id,
        "priority_id": priority_id,
    }
    if parent_id is not None:
        body["parent_id"] = parent_id
    if due_date is not None:
        body["due_date"] = due_date
    if start_date is not None:
        body["start_date"] = start_date

    resp = await client.post(
        f"/api/v1/projects/{project_key}/issues",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_relation(
    client: AsyncClient,
    token: str,
    issue_ref: str,
    issue_to_key: str,
    relation_type: str,
    delay: int | None = None,
) -> tuple[int, dict]:
    body: dict = {"issue_to_key": issue_to_key, "relation_type": relation_type}
    if delay is not None:
        body["delay"] = delay
    resp = await client.post(
        f"/api/v1/issues/{issue_ref}/relations",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.status_code, resp.json()


async def _list_relations(client: AsyncClient, token: str, issue_ref: str) -> tuple[int, list]:
    resp = await client.get(
        f"/api/v1/issues/{issue_ref}/relations",
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.status_code, resp.json()


async def _delete_relation(client: AsyncClient, token: str, relation_id: int) -> int:
    resp = await client.delete(
        f"/api/v1/relations/{relation_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.status_code


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def open_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, is_closed=False)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, open_status: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Task", default_status_id=open_status.id)
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
    proj = ProjectFactory.build(key="REL", identifier="rel-project")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="rel_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def token(admin_client: AsyncClient, admin_user: User) -> str:
    """JWT token for the admin user created in the test DB."""
    return await _login(admin_client, "rel_admin")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_relates_relation(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    open_status: IssueStatus,
    tracker: Tracker,
    priority: IssuePriority,
    project: Project,
    admin_user: User,
) -> None:
    """Creating a 'relates' relation returns 201 with both issue keys."""
    token = await _login(admin_client, admin_user.login)
    a = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "Issue A")
    b = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "Issue B")

    status_code, data = await _create_relation(admin_client, token, a["key"], b["key"], "relates")

    assert status_code == 201, data
    assert data["relation_type"] == "relates"
    assert set([data["issue_from_key"], data["issue_to_key"]]) == {a["key"], b["key"]}


@pytest.mark.asyncio
async def test_create_blocks_relation(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    open_status: IssueStatus,
    tracker: Tracker,
    priority: IssuePriority,
    project: Project,
    admin_user: User,
) -> None:
    """Creating a 'blocks' relation stores it canonically."""
    token = await _login(admin_client, admin_user.login)
    a = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "Blocker")
    b = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "Blocked")

    status_code, data = await _create_relation(admin_client, token, a["key"], b["key"], "blocks")

    assert status_code == 201, data
    assert data["relation_type"] == "blocks"
    assert data["issue_from_key"] == a["key"]
    assert data["issue_to_key"] == b["key"]


@pytest.mark.asyncio
async def test_create_precedes_with_delay_reschedules(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    open_status: IssueStatus,
    tracker: Tracker,
    priority: IssuePriority,
    project: Project,
    admin_user: User,
) -> None:
    """Creating a 'precedes' relation with delay reschedules successor start_date."""
    token = await _login(admin_client, admin_user.login)
    a = await _create_issue(
        admin_client,
        token,
        "REL",
        tracker.id,
        open_status.id,
        priority.id,
        "Predecessor",
        due_date="2026-04-04",
    )
    b = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "Successor")

    status_code, data = await _create_relation(admin_client, token, a["key"], b["key"], "precedes", delay=0)

    assert status_code == 201, data
    assert data["relation_type"] == "precedes"
    assert data["delay"] == 0

    # Verify successor was rescheduled: 2026-04-04 + 0 + 1 = 2026-04-05
    resp = await admin_client.get(
        f"/api/v1/issues/{b['key']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["start_date"] == "2026-04-05"


@pytest.mark.asyncio
async def test_circular_blocks_rejected(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    open_status: IssueStatus,
    tracker: Tracker,
    priority: IssuePriority,
    project: Project,
    admin_user: User,
) -> None:
    """A→blocks→B then B→blocks→A must be rejected with 422."""
    token = await _login(admin_client, admin_user.login)
    a = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "A")
    b = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "B")

    # First relation succeeds
    sc1, _ = await _create_relation(admin_client, token, a["key"], b["key"], "blocks")
    assert sc1 == 201

    # Second relation (B blocks A) creates a cycle → must be rejected
    sc2, data2 = await _create_relation(admin_client, token, b["key"], a["key"], "blocks")
    assert sc2 == 422, data2
    assert "circular" in data2["errors"][0]["message"].lower()


@pytest.mark.asyncio
async def test_circular_precedes_rejected(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    open_status: IssueStatus,
    tracker: Tracker,
    priority: IssuePriority,
    project: Project,
    admin_user: User,
) -> None:
    """A→precedes→B then B→precedes→A must be rejected with 422."""
    token = await _login(admin_client, admin_user.login)
    a = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "Step 1")
    b = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "Step 2")

    sc1, _ = await _create_relation(admin_client, token, a["key"], b["key"], "precedes")
    assert sc1 == 201

    sc2, data2 = await _create_relation(admin_client, token, b["key"], a["key"], "precedes")
    assert sc2 == 422, data2
    assert "circular" in data2["errors"][0]["message"].lower()


@pytest.mark.asyncio
async def test_parent_descendant_relation_rejected(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    open_status: IssueStatus,
    tracker: Tracker,
    priority: IssuePriority,
    project: Project,
    admin_user: User,
) -> None:
    """Relations between a parent and its descendant must be rejected with 422."""
    token = await _login(admin_client, admin_user.login)
    parent = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "Parent")
    child = await _create_issue(
        admin_client,
        token,
        "REL",
        tracker.id,
        open_status.id,
        priority.id,
        "Child",
        parent_id=parent["id"],
    )

    sc, data = await _create_relation(admin_client, token, parent["key"], child["key"], "relates")
    assert sc == 422, data
    assert "descendant" in data["errors"][0]["message"].lower()


@pytest.mark.asyncio
async def test_duplicate_relation_rejected(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    open_status: IssueStatus,
    tracker: Tracker,
    priority: IssuePriority,
    project: Project,
    admin_user: User,
) -> None:
    """Creating the same relation twice must be rejected with 422."""
    token = await _login(admin_client, admin_user.login)
    a = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "Dup A")
    b = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "Dup B")

    sc1, _ = await _create_relation(admin_client, token, a["key"], b["key"], "blocks")
    assert sc1 == 201

    sc2, data2 = await _create_relation(admin_client, token, a["key"], b["key"], "blocks")
    assert sc2 == 422, data2
    assert "already exists" in data2["errors"][0]["message"].lower()


@pytest.mark.asyncio
async def test_delete_relation(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    open_status: IssueStatus,
    tracker: Tracker,
    priority: IssuePriority,
    project: Project,
    admin_user: User,
) -> None:
    """Deleting a relation returns 204 and it no longer appears in the list."""
    token = await _login(admin_client, admin_user.login)
    a = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "Del A")
    b = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "Del B")

    _, rel = await _create_relation(admin_client, token, a["key"], b["key"], "relates")
    rel_id = rel["id"]

    delete_sc = await _delete_relation(admin_client, token, rel_id)
    assert delete_sc == 204

    _, relations = await _list_relations(admin_client, token, a["key"])
    assert all(r["id"] != rel_id for r in relations)


@pytest.mark.asyncio
async def test_delete_nonexistent_relation_returns_404(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    open_status: IssueStatus,
    tracker: Tracker,
    priority: IssuePriority,
    project: Project,
) -> None:
    """Deleting a relation that does not exist returns 404."""
    token = await _login(admin_client, admin_user.login)
    sc = await _delete_relation(admin_client, token, 99999)
    assert sc == 404


@pytest.mark.asyncio
async def test_list_relations_correct_types_per_direction(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    open_status: IssueStatus,
    tracker: Tracker,
    priority: IssuePriority,
    project: Project,
    admin_user: User,
) -> None:
    """List shows 'blocks' from A's perspective and 'blocked' from B's perspective."""
    token = await _login(admin_client, admin_user.login)
    a = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "Dir A")
    b = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "Dir B")

    await _create_relation(admin_client, token, a["key"], b["key"], "blocks")

    # From A's perspective: A blocks B
    _, rels_a = await _list_relations(admin_client, token, a["key"])
    a_types = [r["relation_type"] for r in rels_a]
    assert "blocks" in a_types

    # From B's perspective: B is blocked by A → should show as 'blocked'
    _, rels_b = await _list_relations(admin_client, token, b["key"])
    b_types = [r["relation_type"] for r in rels_b]
    assert "blocked" in b_types


@pytest.mark.asyncio
async def test_reverse_type_normalisation(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    open_status: IssueStatus,
    tracker: Tracker,
    priority: IssuePriority,
    project: Project,
    admin_user: User,
) -> None:
    """Creating a 'follows' relation stores it as 'precedes' with swapped from/to.

    If we say 'B follows A' (B comes after A), the canonical form stored is
    'A precedes B'. The response should reflect this normalisation.
    """
    token = await _login(admin_client, admin_user.login)
    a = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "Norm A")
    b = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "Norm B")

    # Create "B follows A" — should be normalised to "A precedes B"
    sc, data = await _create_relation(admin_client, token, b["key"], a["key"], "follows")
    assert sc == 201, data

    # From B's perspective, B follows A
    _, rels_b = await _list_relations(admin_client, token, b["key"])
    assert any(r["relation_type"] == "follows" for r in rels_b), rels_b

    # From A's perspective, A precedes B
    _, rels_a = await _list_relations(admin_client, token, a["key"])
    assert any(r["relation_type"] == "precedes" for r in rels_a), rels_a


@pytest.mark.asyncio
async def test_self_relation_rejected(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    open_status: IssueStatus,
    tracker: Tracker,
    priority: IssuePriority,
    project: Project,
    admin_user: User,
) -> None:
    """An issue cannot be related to itself — must return 422."""
    token = await _login(admin_client, admin_user.login)
    a = await _create_issue(admin_client, token, "REL", tracker.id, open_status.id, priority.id, "Self")

    sc, data = await _create_relation(admin_client, token, a["key"], a["key"], "relates")
    assert sc == 422, data
    assert "itself" in data["errors"][0]["message"].lower()
