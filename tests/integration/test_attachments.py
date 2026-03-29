"""Integration tests for attachments.

requirements:
- Upload file
- Download file
- Delete file
- List attachments for issue
- ?include=attachments works
"""

from __future__ import annotations

import io

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.attachment import Attachment
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str, password: str = "testpassword") -> str:
    resp = await client.post("/api/v1/auth/login", json={"login": login, "password": password})
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
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project_key}/issues",
        json={
            "project_key": project_key,
            "tracker_id": tracker_id,
            "subject": subject,
            "status_id": status_id,
            "priority_id": priority_id,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _upload_file(
    client: AsyncClient,
    token: str,
    container_type: str,
    container_id: int,
    filename: str = "test.txt",
    content: bytes = b"test file content",
    content_type: str = "text/plain",
    description: str | None = None,
) -> dict:
    files = {"file": (filename, io.BytesIO(content), content_type)}
    data = {"container_type": container_type, "container_id": str(container_id)}
    if description is not None:
        data["description"] = description
    resp = await client.post(
        "/api/v1/attachments",
        files=files,
        data=data,
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp


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
    proj = ProjectFactory.build(key="ATT", identifier="attachment-test")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="attach_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


@pytest_asyncio.fixture
async def second_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="attach_user2", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def second_token(second_user: User, client: AsyncClient) -> str:
    return await _login(client, second_user.login)


# ---------------------------------------------------------------------------
# Tests: upload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_attachment(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """POST /attachments uploads a file and returns attachment metadata."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Upload test"
    )

    resp = await _upload_file(
        client,
        admin_token,
        "Issue",
        issue_data["id"],
        filename="notes.txt",
        content=b"some notes here",
        content_type="text/plain",
        description="My test file",
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()

    assert data["filename"] == "notes.txt"
    assert data["container_type"] == "Issue"
    assert data["container_id"] == issue_data["id"]
    assert data["filesize"] == len(b"some notes here")
    assert data["content_type"] == "text/plain"
    assert data["description"] == "My test file"
    assert data["author"]["id"] == admin_user.id

    # Verify in DB
    result = await db_session.execute(select(Attachment).where(Attachment.id == data["id"]))
    att = result.scalar_one_or_none()
    assert att is not None
    assert att.filename == "notes.txt"


@pytest.mark.asyncio
async def test_upload_file_too_large_rejected(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Files exceeding the 50 MB limit are rejected with 422."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Size test"
    )

    # Create a file that is exactly 1 byte over the limit
    from specivo.services.attachment_service import _get_max_file_size

    big_content = b"x" * (_get_max_file_size() + 1)

    resp = await _upload_file(
        client,
        admin_token,
        "Issue",
        issue_data["id"],
        filename="big.txt",
        content=big_content,
        content_type="text/plain",
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_upload_disallowed_content_type_rejected(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Files with disallowed content types are rejected."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Type test"
    )

    resp = await _upload_file(
        client,
        admin_token,
        "Issue",
        issue_data["id"],
        filename="evil.exe",
        content=b"MZ\x90\x00",
        content_type="application/x-msdownload",
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Tests: get metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_attachment_metadata(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """GET /attachments/{id} returns attachment metadata."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Meta test"
    )

    upload_resp = await _upload_file(
        client,
        admin_token,
        "Issue",
        issue_data["id"],
        filename="meta.txt",
        content=b"metadata test",
    )
    assert upload_resp.status_code == 201, upload_resp.text
    attachment_id = upload_resp.json()["id"]

    resp = await client.get(
        f"/api/v1/attachments/{attachment_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == attachment_id
    assert data["filename"] == "meta.txt"


# ---------------------------------------------------------------------------
# Tests: download
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_attachment(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """GET /attachments/{id}/download returns the file content."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Download test"
    )
    file_content = b"Hello from the attachment download test!"

    upload_resp = await _upload_file(
        client,
        admin_token,
        "Issue",
        issue_data["id"],
        filename="download.txt",
        content=file_content,
        content_type="text/plain",
    )
    assert upload_resp.status_code == 201, upload_resp.text
    attachment_id = upload_resp.json()["id"]

    resp = await client.get(
        f"/api/v1/attachments/{attachment_id}/download",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == file_content


# ---------------------------------------------------------------------------
# Tests: delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_attachment_by_author(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Author can delete their own attachment."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Delete test"
    )

    upload_resp = await _upload_file(
        client,
        admin_token,
        "Issue",
        issue_data["id"],
        filename="to_delete.txt",
        content=b"delete me",
    )
    assert upload_resp.status_code == 201, upload_resp.text
    attachment_id = upload_resp.json()["id"]

    resp = await client.delete(
        f"/api/v1/attachments/{attachment_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 204, resp.text

    # Verify gone from DB
    result = await db_session.execute(select(Attachment).where(Attachment.id == attachment_id))
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_delete_attachment_by_non_author_forbidden(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    second_user: User,
    second_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Non-author non-admin gets 403 when deleting someone else's attachment."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Auth delete test"
    )

    # Admin uploads a file
    upload_resp = await _upload_file(
        client,
        admin_token,
        "Issue",
        issue_data["id"],
        filename="admin_file.txt",
        content=b"admin only",
    )
    assert upload_resp.status_code == 201, upload_resp.text
    attachment_id = upload_resp.json()["id"]

    # second_user tries to delete it
    resp = await client.delete(
        f"/api/v1/attachments/{attachment_id}",
        headers={"Authorization": f"Bearer {second_token}"},
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Tests: list and ?include=attachments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_attachments_via_include(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """GET /issues/{ref}?include=attachments returns attachments inline."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Include attach"
    )
    issue_key = issue_data["key"]

    # Upload two files
    await _upload_file(
        client,
        admin_token,
        "Issue",
        issue_data["id"],
        filename="first.txt",
        content=b"first",
    )
    await _upload_file(
        client,
        admin_token,
        "Issue",
        issue_data["id"],
        filename="second.txt",
        content=b"second",
    )

    resp = await client.get(
        f"/api/v1/issues/{issue_key}?include=attachments",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert "attachments" in data
    assert data["attachments"] is not None
    assert len(data["attachments"]) == 2
    filenames = {a["filename"] for a in data["attachments"]}
    assert filenames == {"first.txt", "second.txt"}


@pytest.mark.asyncio
async def test_include_attachments_not_requested_returns_none(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Without ?include=attachments, the attachments field is null."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "No attach include"
    )
    issue_key = issue_data["key"]

    resp = await client.get(
        f"/api/v1/issues/{issue_key}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data.get("attachments") is None
