"""Integration tests for metadata-enriched attachment search (Phase 2).

Covers:
- Upload attachment then PATCH with metadata JSON -> stored in DB
- Metadata with extracted_text triggers multi-chunk re-indexing
- Search finds attachments by extracted_text and ai_description content
- Metadata validation rejects invalid payloads (missing schema_version)
- Description update preserves metadata field

RED PHASE: The metadata JSONB column, Pydantic schemas, multi-chunk support,
and metadata PATCH endpoint don't exist yet. All tests will fail until
Phase 2 is implemented.
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
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.search import EmbeddingModel, SearchChunk, SearchSource
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SEARCH_URL = "/api/v1/search"


async def _make_user(db: AsyncSession, login: str = "meta_user") -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client: AsyncClient, login: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"login": login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _make_project(
    db: AsyncSession,
    key: str = "MTS",
    identifier: str = "meta-search-project",
    is_public: bool = True,
) -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier, is_public=is_public)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


async def _seed_lookups(
    db: AsyncSession,
) -> tuple[Tracker, IssueStatus, IssuePriority]:
    status = StatusFactory.build(name="New", position=1, is_closed=False)
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="Bug", default_status_id=status.id)
    db.add(tracker)
    priority = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db.add(priority)
    await db.commit()
    await db.refresh(status)
    await db.refresh(tracker)
    await db.refresh(priority)
    return tracker, status, priority


async def _add_manager(db: AsyncSession, project: Project, user: User) -> None:
    role = Role(
        name=f"Manager-{project.key}-{user.id}",
        permissions=["*"],
        builtin=0,
    )
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    mr = MemberRole(member_id=member.id, role_id=role.id)
    db.add(mr)
    await db.commit()


async def _create_mock_model(db: AsyncSession) -> EmbeddingModel:
    model = EmbeddingModel(
        name="test-mock-meta",
        provider="mock",
        model_name="mock-1536",
        dimensions=1536,
        is_default=True,
    )
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return model


async def _create_issue(
    client: AsyncClient,
    project_key: str,
    tracker_id: int,
    subject: str,
) -> dict:
    payload = {
        "project_key": project_key,
        "tracker_id": tracker_id,
        "subject": subject,
    }
    resp = await client.post(f"/api/v1/projects/{project_key}/issues", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _upload_attachment(
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
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    return await _make_project(db_session)


@pytest_asyncio.fixture
async def lookups(db_session: AsyncSession) -> tuple[Tracker, IssueStatus, IssuePriority]:
    return await _seed_lookups(db_session)


@pytest_asyncio.fixture
async def meta_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="meta_search_user")


@pytest_asyncio.fixture
async def mock_model(db_session: AsyncSession) -> EmbeddingModel:
    return await _create_mock_model(db_session)


@pytest_asyncio.fixture
async def authed_client(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
    meta_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
) -> AsyncClient:
    """Client authenticated as a manager with mock embedding model."""
    await _add_manager(db_session, project, meta_user)
    token = await _login(client, meta_user.login)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------------------------------------------------------------------------
# Tests: metadata storage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_attachment_with_metadata(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    meta_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Upload attachment, then PATCH with metadata JSON -> metadata stored in DB."""
    tracker, _, _ = lookups
    issue = await _create_issue(authed_client, project.key, tracker.id, "Metadata storage test")
    token = await _login(authed_client, meta_user.login)

    att = await _upload_attachment(
        authed_client,
        token,
        "Issue",
        issue["id"],
        filename="auth-guide.pdf",
        content=b"PDF fake data",
        content_type="application/pdf",
        description="Authentication system guide",
    )

    # PATCH with metadata
    resp = await authed_client.patch(
        f"/api/v1/attachments/{att['id']}",
        json={
            "metadata": {
                "schema_version": 1,
                "source": "pdf_extract",
                "generated_at": "2026-03-29T10:00:00Z",
                "generated_by": "system:celery",
                "pdf": {"page_count": 12, "title": "Auth Guide", "author": "Boris S."},
                "extracted_text": "Section 3.2 describes the refresh token rotation...",
            }
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    # Verify metadata stored in DB
    result = await db_session.execute(select(Attachment).where(Attachment.id == att["id"]))
    db_att = result.scalar_one()
    assert db_att.metadata is not None
    assert db_att.metadata["schema_version"] == 1
    assert db_att.metadata["source"] == "pdf_extract"
    assert db_att.metadata["pdf"]["page_count"] == 12
    assert "refresh token rotation" in db_att.metadata["extracted_text"]


@pytest.mark.asyncio
async def test_metadata_update_triggers_reindex(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    meta_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Set metadata with extracted_text -> SearchChunk count increases (multi-chunk)."""
    tracker, _, _ = lookups
    issue = await _create_issue(authed_client, project.key, tracker.id, "Reindex metadata test")
    token = await _login(authed_client, meta_user.login)

    att = await _upload_attachment(
        authed_client,
        token,
        "Issue",
        issue["id"],
        filename="long-report.pdf",
        content=b"PDF data",
        content_type="application/pdf",
        description="Detailed report",
    )

    # Before metadata: should have 1 chunk (filename + description)
    source_result = await db_session.execute(
        select(SearchSource).where(
            SearchSource.source_type == "attachment",
            SearchSource.entity_id == att["id"],
        )
    )
    source = source_result.scalar_one()
    chunks_before = await db_session.execute(select(SearchChunk).where(SearchChunk.source_id == source.id))
    count_before = len(chunks_before.scalars().all())
    assert count_before == 1

    # PATCH with metadata containing long extracted_text -> multiple chunks
    long_extracted = ("This document covers authentication patterns in detail. " * 40).strip()
    resp = await authed_client.patch(
        f"/api/v1/attachments/{att['id']}",
        json={
            "metadata": {
                "schema_version": 1,
                "source": "pdf_extract",
                "generated_at": "2026-03-29T10:00:00Z",
                "generated_by": "system:celery",
                "pdf": {"page_count": 30},
                "extracted_text": long_extracted,
            }
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    # After metadata: should have more chunks
    chunks_after = await db_session.execute(select(SearchChunk).where(SearchChunk.source_id == source.id))
    count_after = len(chunks_after.scalars().all())
    assert count_after > count_before


# ---------------------------------------------------------------------------
# Tests: search by metadata content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_finds_attachment_by_extracted_text(
    authed_client: AsyncClient,
    project: Project,
    meta_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Set PDF metadata with extracted_text -> search for words from extracted text -> finds it."""
    tracker, _, _ = lookups
    issue = await _create_issue(authed_client, project.key, tracker.id, "Extracted text search")
    token = await _login(authed_client, meta_user.login)

    att = await _upload_attachment(
        authed_client,
        token,
        "Issue",
        issue["id"],
        filename="token-rotation.pdf",
        content=b"PDF data",
        content_type="application/pdf",
        description="Token management guide",
    )

    # Add metadata with extracted_text
    resp = await authed_client.patch(
        f"/api/v1/attachments/{att['id']}",
        json={
            "metadata": {
                "schema_version": 1,
                "source": "pdf_extract",
                "generated_at": "2026-03-29T10:00:00Z",
                "generated_by": "system:celery",
                "pdf": {"page_count": 8, "title": "Token Rotation"},
                "extracted_text": (
                    "Section 3.2 describes the refresh token rotation strategy. "
                    "When an access token expires, the client presents the refresh "
                    "token to obtain a new access token. The old refresh token is "
                    "immediately invalidated to prevent replay attacks."
                ),
            }
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    # Search for words that only appear in extracted_text
    search_resp = await authed_client.get(SEARCH_URL, params={"q": "replay attacks refresh token"})
    assert search_resp.status_code == 200, search_resp.text
    data = search_resp.json()
    attachment_items = [i for i in data["items"] if i["result_type"] == "attachment"]
    assert len(attachment_items) >= 1


@pytest.mark.asyncio
async def test_search_finds_attachment_by_ai_description(
    authed_client: AsyncClient,
    project: Project,
    meta_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Set image metadata with ai_description -> search for AI description words -> finds it."""
    tracker, _, _ = lookups
    issue = await _create_issue(authed_client, project.key, tracker.id, "AI description search")
    token = await _login(authed_client, meta_user.login)

    att = await _upload_attachment(
        authed_client,
        token,
        "Issue",
        issue["id"],
        filename="architecture-diagram.png",
        content=b"PNG data",
        content_type="image/png",
        description="System diagram",
    )

    # Add metadata with AI-generated description
    resp = await authed_client.patch(
        f"/api/v1/attachments/{att['id']}",
        json={
            "metadata": {
                "schema_version": 1,
                "source": "ai_describe",
                "generated_at": "2026-03-29T10:00:00Z",
                "generated_by": "agent:claude-session-xyz",
                "image": {"width": 1920, "height": 1080, "format": "PNG"},
                "ai_description": (
                    "A flowchart showing microservice communication patterns "
                    "with circuit breaker and bulkhead isolation strategies"
                ),
            }
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    # Search for words from AI description (not in original description or filename)
    search_resp = await authed_client.get(SEARCH_URL, params={"q": "bulkhead isolation circuit breaker"})
    assert search_resp.status_code == 200, search_resp.text
    data = search_resp.json()
    attachment_items = [i for i in data["items"] if i["result_type"] == "attachment"]
    assert len(attachment_items) >= 1


# ---------------------------------------------------------------------------
# Tests: validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_validation_rejects_invalid(
    authed_client: AsyncClient,
    project: Project,
    meta_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """PATCH with invalid metadata (missing schema_version) -> 422 error."""
    tracker, _, _ = lookups
    issue = await _create_issue(authed_client, project.key, tracker.id, "Validation test")
    token = await _login(authed_client, meta_user.login)

    att = await _upload_attachment(
        authed_client,
        token,
        "Issue",
        issue["id"],
        filename="invalid-meta.txt",
        content=b"data",
    )

    # Missing schema_version -> should be rejected
    resp = await authed_client.patch(
        f"/api/v1/attachments/{att['id']}",
        json={
            "metadata": {
                "source": "upload",
                "generated_at": "2026-03-29T10:00:00Z",
                "generated_by": "user:1",
                # schema_version intentionally omitted
            }
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests: metadata preservation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_preserved_on_description_update(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    meta_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Update description via PATCH -> metadata field unchanged."""
    tracker, _, _ = lookups
    issue = await _create_issue(authed_client, project.key, tracker.id, "Preservation test")
    token = await _login(authed_client, meta_user.login)

    att = await _upload_attachment(
        authed_client,
        token,
        "Issue",
        issue["id"],
        filename="preserved.pdf",
        content=b"PDF data",
        content_type="application/pdf",
        description="Original description",
    )

    # Set metadata first
    meta_resp = await authed_client.patch(
        f"/api/v1/attachments/{att['id']}",
        json={
            "metadata": {
                "schema_version": 1,
                "source": "pdf_extract",
                "generated_at": "2026-03-29T10:00:00Z",
                "generated_by": "system:celery",
                "pdf": {"page_count": 5, "title": "Preserved Doc"},
                "extracted_text": "Important extracted content that must survive.",
            }
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert meta_resp.status_code == 200, meta_resp.text

    # Now update only the description (no metadata in payload)
    desc_resp = await authed_client.patch(
        f"/api/v1/attachments/{att['id']}",
        json={"description": "Updated description only"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert desc_resp.status_code == 200, desc_resp.text

    # Verify metadata is still intact
    result = await db_session.execute(select(Attachment).where(Attachment.id == att["id"]))
    db_att = result.scalar_one()
    assert db_att.metadata is not None
    assert db_att.metadata["schema_version"] == 1
    assert db_att.metadata["pdf"]["page_count"] == 5
    assert "Important extracted content" in db_att.metadata["extracted_text"]
    # And the description was updated
    assert db_att.description == "Updated description only"
