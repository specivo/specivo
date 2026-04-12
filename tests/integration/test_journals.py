"""Integration tests for journals: field change tracking and comments.

(journals) requirements:
- Issue update creates journal with field changes
- Description change stores full old/new text
- Add comment (notes only)
- List journals ordered by created_at
- ?include=journals works
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.models.journal import Journal, JournalDetail
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str, password: str = "testpassword") -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _create_issue_via_api(
    client: AsyncClient,
    token: str,
    project_key: str,
    tracker_id: int,
    status_id: int,
    priority_id: int,
    subject: str,
    description: str | None = None,
) -> dict:
    payload = {
        "project_key": project_key,
        "tracker_id": tracker_id,
        "subject": subject,
        "status_id": status_id,
        "priority_id": priority_id,
    }
    if description is not None:
        payload["description"] = description
    resp = await client.post(
        f"/api/v1/projects/{project_key}/issues/",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def open_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def closed_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="Closed", position=2, category="closed")
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
    proj = ProjectFactory.build(key="JRN", identifier="journal-test")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="journal_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


# ---------------------------------------------------------------------------
# Tests: journal creation on update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_update_creates_journal(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    closed_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Updating an issue creates a journal with the changed fields recorded."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Initial subject"
    )
    issue_key = issue_data["key"]
    lock_version = issue_data["lock_version"]

    resp = await client.patch(
        f"/api/v1/issues/{issue_key}/",
        json={"status_id": closed_status.id, "lock_version": lock_version},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text

    # Verify journal was created
    result = await db_session.execute(
        select(Journal).options(selectinload(Journal.details)).where(Journal.issue_id == issue_data["id"])
    )
    journals = list(result.scalars().all())
    assert len(journals) == 1, "Expected exactly one journal from the update"

    journal = journals[0]
    assert journal.sequence == 1
    assert journal.user_id == admin_user.id

    # Check that status_id change is recorded in details
    status_detail = next((d for d in journal.details if d.prop_key == "status_id"), None)
    assert status_detail is not None, "Expected a journal detail for status_id"
    assert status_detail.old_value == str(open_status.id)
    assert status_detail.new_value == str(closed_status.id)


@pytest.mark.asyncio
async def test_description_change_stores_full_text(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Description changes store the FULL old and new text (not truncated)."""
    old_desc = "# Original\n\nThis is the original description with plenty of text."
    new_desc = "# Updated\n\nThis is the completely rewritten description."

    issue_data = await _create_issue_via_api(
        client,
        admin_token,
        project.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Description test",
        description=old_desc,
    )
    issue_key = issue_data["key"]
    lock_version = issue_data["lock_version"]

    resp = await client.patch(
        f"/api/v1/issues/{issue_key}/",
        json={"description": new_desc, "lock_version": lock_version},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text

    result = await db_session.execute(
        select(JournalDetail)
        .join(Journal, JournalDetail.journal_id == Journal.id)
        .where(Journal.issue_id == issue_data["id"], JournalDetail.prop_key == "description")
        .order_by(Journal.sequence)
    )
    details = list(result.scalars().all())

    # First journal: initial description (None → old_desc)
    # Second journal: description edit (old_desc → new_desc)
    assert len(details) == 2, f"Expected 2 description details (initial + edit), got {len(details)}"

    assert details[0].old_value is None
    assert details[0].new_value == old_desc

    # Full text must be stored — not a diff, not truncated
    assert details[1].old_value == old_desc
    assert details[1].new_value == new_desc


@pytest.mark.asyncio
async def test_no_journal_created_for_noop_update(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """An update that changes nothing (same values) does not create a journal entry."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "No-op issue"
    )
    issue_key = issue_data["key"]
    lock_version = issue_data["lock_version"]

    # Update with the same status_id (no actual change)
    resp = await client.patch(
        f"/api/v1/issues/{issue_key}/",
        json={"status_id": open_status.id, "lock_version": lock_version},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text

    result = await db_session.execute(select(Journal).where(Journal.issue_id == issue_data["id"]))
    journals = list(result.scalars().all())
    assert len(journals) == 0, "No journal should be created for a no-op update"


@pytest.mark.asyncio
async def test_multiple_fields_create_one_journal_with_multiple_details(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    closed_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Updating multiple fields at once creates one journal with multiple details."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Multi-field"
    )
    issue_key = issue_data["key"]
    lock_version = issue_data["lock_version"]

    resp = await client.patch(
        f"/api/v1/issues/{issue_key}/",
        json={
            "status_id": closed_status.id,
            "subject": "Updated subject",
            "done_ratio": 100,
            "lock_version": lock_version,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text

    result = await db_session.execute(
        select(Journal).options(selectinload(Journal.details)).where(Journal.issue_id == issue_data["id"])
    )
    journals = list(result.scalars().all())
    assert len(journals) == 1, "Expected exactly one journal"

    prop_keys = {d.prop_key for d in journals[0].details}
    assert "status_id" in prop_keys
    assert "subject" in prop_keys
    assert "done_ratio" in prop_keys


# ---------------------------------------------------------------------------
# Tests: initial description versioning (SPECIVO-9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_created_with_description_stores_initial_version(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Creating an issue with a description stores it as the initial version (version 0)."""
    desc = "## Overview\n\nThis is the initial description."

    issue_data = await _create_issue_via_api(
        client,
        admin_token,
        project.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Initial version test",
        description=desc,
    )

    result = await db_session.execute(
        select(Journal).options(selectinload(Journal.details)).where(Journal.issue_id == issue_data["id"])
    )
    journals = list(result.scalars().all())
    assert len(journals) == 1, "Expected one journal for the initial description"

    journal = journals[0]
    assert journal.sequence == 1
    assert journal.user_id == admin_user.id
    assert journal.notes is None  # no comment, just the description snapshot

    desc_detail = next((d for d in journal.details if d.prop_key == "description"), None)
    assert desc_detail is not None, "Expected a description detail in the initial journal"
    assert desc_detail.old_value is None  # no previous description
    assert desc_detail.new_value == desc


@pytest.mark.asyncio
async def test_issue_created_without_description_has_no_initial_journal(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Creating an issue without a description does not create an initial journal."""
    issue_data = await _create_issue_via_api(
        client,
        admin_token,
        project.key,
        tracker.id,
        open_status.id,
        priority.id,
        "No description issue",
    )

    result = await db_session.execute(select(Journal).where(Journal.issue_id == issue_data["id"]))
    journals = list(result.scalars().all())
    assert len(journals) == 0, "No journal should be created for issue without description"


@pytest.mark.asyncio
async def test_first_description_edit_has_diff_baseline(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """The first description edit can diff against the initial version."""
    original = "Original description text."
    updated = "Updated description text."

    issue_data = await _create_issue_via_api(
        client,
        admin_token,
        project.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Diff baseline test",
        description=original,
    )
    issue_key = issue_data["key"]
    lock_version = issue_data["lock_version"]

    resp = await client.patch(
        f"/api/v1/issues/{issue_key}/",
        json={"description": updated, "lock_version": lock_version},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text

    # Should have 2 journals: initial version + edit
    result = await db_session.execute(
        select(JournalDetail)
        .join(Journal, JournalDetail.journal_id == Journal.id)
        .where(Journal.issue_id == issue_data["id"], JournalDetail.prop_key == "description")
        .order_by(Journal.sequence)
    )
    details = list(result.scalars().all())
    assert len(details) == 2

    # Initial: None → original
    assert details[0].old_value is None
    assert details[0].new_value == original

    # Edit: original → updated (this is what the diff view uses)
    assert details[1].old_value == original
    assert details[1].new_value == updated


# ---------------------------------------------------------------------------
# Tests: add_comment endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_comment_creates_notes_only_journal(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """POST /issues/{ref}/journals creates a notes-only journal entry."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Comment test"
    )
    issue_key = issue_data["key"]

    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "This is a comment on the issue."},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()

    assert data["notes"] == "This is a comment on the issue."
    assert data["sequence"] == 1
    assert data["details"] == []
    assert data["issue_id"] == issue_data["id"]

    # Verify in DB
    result = await db_session.execute(select(Journal).where(Journal.issue_id == issue_data["id"]))
    journals = list(result.scalars().all())
    assert len(journals) == 1
    assert journals[0].notes == "This is a comment on the issue."


@pytest.mark.asyncio
async def test_add_comment_sequence_increments(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Each comment gets an incrementing sequence number."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Seq test"
    )
    issue_key = issue_data["key"]

    for i in range(1, 4):
        resp = await client.post(
            f"/api/v1/issues/{issue_key}/journals/",
            json={"notes": f"Comment {i}"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["sequence"] == i


# ---------------------------------------------------------------------------
# Tests: list journals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_journals_ordered_by_created_at(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Journals are returned ordered by created_at ascending."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Order test"
    )
    issue_key = issue_data["key"]

    for i in range(3):
        await client.post(
            f"/api/v1/issues/{issue_key}/journals/",
            json={"notes": f"Comment {i}"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    resp = await client.get(
        f"/api/v1/issues/{issue_key}/?include=journals",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    journals = resp.json()["journals"]
    assert journals is not None
    assert len(journals) == 3
    sequences = [j["sequence"] for j in journals]
    assert sequences == sorted(sequences), "Journals must be returned in sequence order"


# ---------------------------------------------------------------------------
# Tests: ?include=journals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_journals_on_issue_get(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """GET /issues/{ref}?include=journals returns journals list."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Include test"
    )
    issue_key = issue_data["key"]

    await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "First comment"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    resp = await client.get(
        f"/api/v1/issues/{issue_key}/?include=journals",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert "journals" in data
    assert data["journals"] is not None
    assert len(data["journals"]) == 1
    assert data["journals"][0]["notes"] == "First comment"


@pytest.mark.asyncio
async def test_include_journals_not_requested_returns_none(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Without ?include=journals, the journals field is null."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "No include"
    )
    issue_key = issue_data["key"]

    resp = await client.get(
        f"/api/v1/issues/{issue_key}/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data.get("journals") is None


@pytest.mark.asyncio
async def test_include_journals_shows_field_changes(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    closed_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """?include=journals shows journals with journal details for field changes."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Field changes"
    )
    issue_key = issue_data["key"]
    lock_version = issue_data["lock_version"]

    await client.patch(
        f"/api/v1/issues/{issue_key}/",
        json={"status_id": closed_status.id, "lock_version": lock_version},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    resp = await client.get(
        f"/api/v1/issues/{issue_key}/?include=journals",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    journals = resp.json()["journals"]
    assert len(journals) == 1

    details = journals[0]["details"]
    assert len(details) >= 1
    status_detail = next((d for d in details if d["prop_key"] == "status_id"), None)
    assert status_detail is not None
    assert status_detail["old_value"] == str(open_status.id)
    assert status_detail["new_value"] == str(closed_status.id)


@pytest.mark.asyncio
async def test_metadata_change_is_journaled(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Changing issue_metadata via PATCH creates a journal detail."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Meta change"
    )
    issue_key = issue_data["key"]
    lock_version = issue_data["lock_version"]

    resp = await client.patch(
        f"/api/v1/issues/{issue_key}/",
        json={"metadata": {"severity": "high"}, "lock_version": lock_version},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text

    result = await db_session.execute(
        select(Journal).options(selectinload(Journal.details)).where(Journal.issue_id == issue_data["id"])
    )
    journals = list(result.scalars().all())
    # Find any journal carrying an issue_metadata detail
    meta_details = [
        d for j in journals for d in j.details if d.prop_key == "issue_metadata"
    ]
    assert len(meta_details) == 1
    assert meta_details[0].old_value in (None, "{}")
    assert "severity" in meta_details[0].new_value
    assert "high" in meta_details[0].new_value
