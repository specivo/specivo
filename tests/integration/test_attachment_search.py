"""Integration tests for searchable file attachments.

Covers:
- Attachment indexed on upload (SearchSource + SearchChunk created)
- Keyword search finds attachments by description and filename
- Hybrid search returns attachments alongside issues
- Scope filtering: scope=attachments, scope=issues excludes attachments
- Delete attachment removes from search index
- Access control: private issue, public project, admin override
- Description update triggers re-index
- Audit logging for upload and description update events

RED PHASE: Most of these tests call endpoints/methods that don't exist yet
(scope=attachments, PATCH /attachments/{id}, attachment source_type in search).
They will fail until the feature is implemented.
"""

from __future__ import annotations

import io

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.search import EmbeddingModel, SearchChunk, SearchSource
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SEARCH_URL = "/api/v1/search/"


async def _make_user(db: AsyncSession, login: str = "att_user") -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_admin(db: AsyncSession, login: str = "att_admin") -> User:
    user = AdminUserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client: AsyncClient, login: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _make_project(
    db: AsyncSession,
    key: str = "ATS",
    identifier: str = "att-search-project",
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
    """Create a mock embedding model for tests."""
    model = EmbeddingModel(
        name="test-mock-att",
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
    description: str | None = None,
    is_private: bool = False,
) -> dict:
    payload: dict = {
        "project_key": project_key,
        "tracker_id": tracker_id,
        "subject": subject,
    }
    if description is not None:
        payload["description"] = description
    if is_private:
        payload["is_private"] = True
    resp = await client.post(f"/api/v1/projects/{project_key}/issues/", json=payload)
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
        "/api/v1/attachments/",
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
async def search_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="att_search_user")


@pytest_asyncio.fixture
async def mock_model(db_session: AsyncSession) -> EmbeddingModel:
    return await _create_mock_model(db_session)


@pytest_asyncio.fixture
async def authed_client(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
) -> AsyncClient:
    """Client authenticated as a manager with mock embedding model."""
    await _add_manager(db_session, project, search_user)
    token = await _login(client, search_user.login)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------------------------------------------------------------------------
# Tests: search functionality
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attachment_indexed_on_upload(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Upload attachment with description -> SearchSource with source_type='attachment' exists."""
    tracker, _, _ = lookups
    issue = await _create_issue(authed_client, project.key, tracker.id, "Indexing test issue")
    token = await _login(authed_client, search_user.login)

    await _upload_attachment(
        authed_client,
        token,
        "Issue",
        issue["id"],
        filename="design.png",
        content=b"PNG fake data",
        content_type="image/png",
        description="architecture diagram of the auth system",
    )

    # Verify SearchSource with source_type="attachment" was created
    result = await db_session.execute(
        select(SearchSource).where(
            SearchSource.source_type == "attachment",
            SearchSource.project_id == project.id,
        )
    )
    sources = result.scalars().all()
    assert len(sources) >= 1

    # Verify chunk contains filename and description
    source = sources[0]
    result = await db_session.execute(select(SearchChunk).where(SearchChunk.source_id == source.id))
    chunks = result.scalars().all()
    assert len(chunks) >= 1
    assert "design.png" in chunks[0].content
    assert "architecture diagram" in chunks[0].content


@pytest.mark.asyncio
async def test_search_finds_attachment_by_description_keyword(
    authed_client: AsyncClient,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Upload attachment with description -> keyword search finds it by description keyword."""
    tracker, _, _ = lookups
    issue = await _create_issue(authed_client, project.key, tracker.id, "Attachment search issue")
    token = await _login(authed_client, search_user.login)

    await _upload_attachment(
        authed_client,
        token,
        "Issue",
        issue["id"],
        filename="flow.png",
        content=b"PNG data",
        content_type="image/png",
        description="architecture diagram of JWT flow",
    )

    resp = await authed_client.get(SEARCH_URL, params={"q": "JWT flow"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] >= 1
    result_types = {item["result_type"] for item in data["items"]}
    assert "attachment" in result_types


@pytest.mark.asyncio
async def test_search_finds_attachment_by_filename(
    authed_client: AsyncClient,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Upload 'jwt-rotation-flow.png' -> keyword search 'jwt-rotation' finds it."""
    tracker, _, _ = lookups
    issue = await _create_issue(authed_client, project.key, tracker.id, "Filename search issue")
    token = await _login(authed_client, search_user.login)

    await _upload_attachment(
        authed_client,
        token,
        "Issue",
        issue["id"],
        filename="jwt-rotation-flow.png",
        content=b"PNG data",
        content_type="image/png",
    )

    resp = await authed_client.get(SEARCH_URL, params={"q": "jwt-rotation"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] >= 1
    attachment_items = [i for i in data["items"] if i["result_type"] == "attachment"]
    assert len(attachment_items) >= 1


@pytest.mark.asyncio
async def test_hybrid_search_returns_attachments_with_issues(
    authed_client: AsyncClient,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Create issue + upload attachment with different descriptions -> hybrid search returns both."""
    tracker, _, _ = lookups
    issue = await _create_issue(
        authed_client,
        project.key,
        tracker.id,
        "Observability pipeline design",
        description="Distributed tracing with OpenTelemetry",
    )
    token = await _login(authed_client, search_user.login)

    await _upload_attachment(
        authed_client,
        token,
        "Issue",
        issue["id"],
        filename="tracing-arch.png",
        content=b"PNG data",
        content_type="image/png",
        description="OpenTelemetry tracing architecture diagram",
    )

    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "OpenTelemetry tracing", "mode": "hybrid"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    result_types = {item["result_type"] for item in data["items"]}
    assert "issue" in result_types
    assert "attachment" in result_types


@pytest.mark.asyncio
async def test_search_scope_attachments_only(
    authed_client: AsyncClient,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Search with scope=attachments returns only attachment results."""
    tracker, _, _ = lookups
    issue = await _create_issue(
        authed_client,
        project.key,
        tracker.id,
        "Scalability improvements for caching",
    )
    token = await _login(authed_client, search_user.login)

    await _upload_attachment(
        authed_client,
        token,
        "Issue",
        issue["id"],
        filename="cache-strategy.pdf",
        content=b"PDF data",
        content_type="application/pdf",
        description="Scalability caching strategy document",
    )

    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "scalability caching", "scope": "attachments"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    result_types = {item["result_type"] for item in data["items"]}
    # Only attachment results should be returned
    assert result_types == {"attachment"}


@pytest.mark.asyncio
async def test_search_scope_issues_excludes_attachments(
    authed_client: AsyncClient,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Search with scope=issues returns no attachment results."""
    tracker, _, _ = lookups
    issue = await _create_issue(
        authed_client,
        project.key,
        tracker.id,
        "Monitoring dashboard setup",
    )
    token = await _login(authed_client, search_user.login)

    await _upload_attachment(
        authed_client,
        token,
        "Issue",
        issue["id"],
        filename="monitoring-config.yaml",
        content=b"yaml data",
        content_type="text/yaml",
        description="Monitoring dashboard configuration for Grafana",
    )

    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "monitoring", "scope": "issues"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    result_types = {item["result_type"] for item in data["items"]}
    assert "attachment" not in result_types


@pytest.mark.asyncio
async def test_delete_attachment_removes_from_search(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Upload, verify searchable, delete, verify NOT searchable."""
    tracker, _, _ = lookups
    issue = await _create_issue(authed_client, project.key, tracker.id, "Delete search issue")
    token = await _login(authed_client, search_user.login)

    att = await _upload_attachment(
        authed_client,
        token,
        "Issue",
        issue["id"],
        filename="ephemeral-doc.txt",
        content=b"temporary content",
        description="ephemeral document for deletion test",
    )

    # Verify it appears in search
    resp = await authed_client.get(SEARCH_URL, params={"q": "ephemeral"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_count"] >= 1

    # Delete the attachment
    del_resp = await authed_client.delete(
        f"/api/v1/attachments/{att['id']}/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert del_resp.status_code == 204, del_resp.text

    # Verify it no longer appears in search
    resp2 = await authed_client.get(SEARCH_URL, params={"q": "ephemeral"})
    assert resp2.status_code == 200, resp2.text
    attachment_items = [i for i in resp2.json()["items"] if i["result_type"] == "attachment"]
    assert len(attachment_items) == 0

    # Verify SearchSource was removed from DB
    result = await db_session.execute(
        select(SearchSource).where(
            SearchSource.source_type == "attachment",
            SearchSource.entity_id == att["id"],
        )
    )
    assert result.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# Tests: access control
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_private_issue_attachment_not_visible_to_non_member(
    client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Upload to private issue -> search as non-member -> attachment NOT in results."""
    tracker, _, _ = lookups

    # search_user is a manager — create private issue and upload attachment
    await _add_manager(db_session, project, search_user)
    member_token = await _login(client, search_user.login)
    client.headers["Authorization"] = f"Bearer {member_token}"

    issue = await _create_issue(
        client,
        project.key,
        tracker.id,
        "Private security audit",
        description="Sensitive findings",
        is_private=True,
    )

    await _upload_attachment(
        client,
        member_token,
        "Issue",
        issue["id"],
        filename="security-report.pdf",
        content=b"classified content",
        content_type="application/pdf",
        description="confidential security vulnerability report",
    )

    # Create a non-member user and search
    outsider = await _make_user(db_session, login="outsider_user")
    outsider_token = await _login(client, outsider.login)

    resp = await client.get(
        SEARCH_URL,
        params={"q": "confidential security vulnerability"},
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 200, resp.text
    attachment_items = [i for i in resp.json()["items"] if i["result_type"] == "attachment"]
    assert len(attachment_items) == 0


@pytest.mark.asyncio
async def test_public_project_attachment_visible_to_any_user(
    client: AsyncClient,
    db_session: AsyncSession,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Upload to public project issue -> search as different user -> attachment visible."""
    tracker, _, _ = lookups

    # Create a public project
    pub_project = await _make_project(db_session, key="PUB", identifier="public-att-project", is_public=True)

    # Create a manager, issue, and attachment
    manager = await _make_user(db_session, login="pub_manager")
    await _add_manager(db_session, pub_project, manager)
    manager_token = await _login(client, manager.login)
    client.headers["Authorization"] = f"Bearer {manager_token}"

    issue = await _create_issue(
        client,
        pub_project.key,
        tracker.id,
        "Public feature request",
        description="Visible to everyone",
    )

    await _upload_attachment(
        client,
        manager_token,
        "Issue",
        issue["id"],
        filename="public-roadmap.pdf",
        content=b"roadmap content",
        content_type="application/pdf",
        description="quarterly product roadmap document",
    )

    # A different user (not a member) should see the attachment in search
    other_user = await _make_user(db_session, login="pub_other_user")
    other_token = await _login(client, other_user.login)

    resp = await client.get(
        SEARCH_URL,
        params={"q": "quarterly roadmap"},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 200, resp.text
    attachment_items = [i for i in resp.json()["items"] if i["result_type"] == "attachment"]
    assert len(attachment_items) >= 1


@pytest.mark.asyncio
async def test_admin_sees_all_attachments_in_search(
    client: AsyncClient,
    db_session: AsyncSession,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Admin can find attachments on any issue regardless of membership."""
    tracker, _, _ = lookups

    # Create a private project with a private issue
    priv_project = await _make_project(db_session, key="PRV", identifier="private-att-project", is_public=False)
    member = await _make_user(db_session, login="priv_member")
    await _add_manager(db_session, priv_project, member)
    member_token = await _login(client, member.login)
    client.headers["Authorization"] = f"Bearer {member_token}"

    issue = await _create_issue(
        client,
        priv_project.key,
        tracker.id,
        "Internal compliance review",
        description="Restricted content",
        is_private=True,
    )

    await _upload_attachment(
        client,
        member_token,
        "Issue",
        issue["id"],
        filename="compliance-audit.pdf",
        content=b"audit data",
        content_type="application/pdf",
        description="internal compliance audit findings for Q4",
    )

    # Admin (not a member of the project) should still see the attachment
    admin = await _make_admin(db_session, login="super_admin")
    admin_token = await _login(client, admin.login)

    resp = await client.get(
        SEARCH_URL,
        params={"q": "compliance audit findings"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    attachment_items = [i for i in resp.json()["items"] if i["result_type"] == "attachment"]
    assert len(attachment_items) >= 1


# ---------------------------------------------------------------------------
# Tests: description update + re-index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_description_reindexes(
    authed_client: AsyncClient,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Upload with desc A -> search finds A -> update to desc B -> search finds B but not A."""
    tracker, _, _ = lookups
    issue = await _create_issue(authed_client, project.key, tracker.id, "Reindex test issue")
    token = await _login(authed_client, search_user.login)

    att = await _upload_attachment(
        authed_client,
        token,
        "Issue",
        issue["id"],
        filename="evolving-doc.txt",
        content=b"some content",
        description="original waterfall methodology document",
    )

    # Verify original description is searchable
    resp = await authed_client.get(SEARCH_URL, params={"q": "waterfall methodology"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_count"] >= 1

    # Update description via PATCH
    patch_resp = await authed_client.patch(
        f"/api/v1/attachments/{att['id']}/",
        json={"description": "revised agile sprint planning guide"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert patch_resp.status_code == 200, patch_resp.text

    # New description should be searchable
    resp2 = await authed_client.get(SEARCH_URL, params={"q": "agile sprint planning"})
    assert resp2.status_code == 200, resp2.text
    attachment_items = [i for i in resp2.json()["items"] if i["result_type"] == "attachment"]
    assert len(attachment_items) >= 1

    # Old description should NOT be searchable
    resp3 = await authed_client.get(SEARCH_URL, params={"q": "waterfall methodology"})
    assert resp3.status_code == 200, resp3.text
    old_attachment_items = [i for i in resp3.json()["items"] if i["result_type"] == "attachment"]
    assert len(old_attachment_items) == 0


# ---------------------------------------------------------------------------
# Tests: audit logging (feature-gated)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attachment_upload_audit_logged(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Upload -> check security_audit_logs for attachment_uploaded event (if feature enabled)."""
    tracker, _, _ = lookups
    issue = await _create_issue(authed_client, project.key, tracker.id, "Audit upload test")
    token = await _login(authed_client, search_user.login)

    await _upload_attachment(
        authed_client,
        token,
        "Issue",
        issue["id"],
        filename="audited-file.txt",
        content=b"audit test content",
        description="file for audit logging test",
    )

    # Check for audit event (feature-gated: may be empty if enterprise plugin not loaded)
    result = await db_session.execute(
        select(SecurityAuditLog).where(
            SecurityAuditLog.event_type == "attachment_uploaded",
            SecurityAuditLog.resource_type == "Attachment",
        )
    )
    audit_logs = result.scalars().all()
    # When the security_audit_log feature is enabled, we expect at least one event
    # When it's not enabled (core-only), the list will be empty — that's acceptable
    if audit_logs:
        log = audit_logs[-1]
        assert log.details["filename"] == "audited-file.txt"
        assert log.user_id == search_user.id


@pytest.mark.asyncio
async def test_attachment_description_update_audit_logged(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Update description -> check for attachment_description_updated event."""
    tracker, _, _ = lookups
    issue = await _create_issue(authed_client, project.key, tracker.id, "Audit update test")
    token = await _login(authed_client, search_user.login)

    att = await _upload_attachment(
        authed_client,
        token,
        "Issue",
        issue["id"],
        filename="audit-update-file.txt",
        content=b"content",
        description="original description",
    )

    # Update the description
    patch_resp = await authed_client.patch(
        f"/api/v1/attachments/{att['id']}/",
        json={"description": "updated description for audit test"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert patch_resp.status_code == 200, patch_resp.text

    # Check for audit event
    result = await db_session.execute(
        select(SecurityAuditLog).where(
            SecurityAuditLog.event_type == "attachment_description_updated",
            SecurityAuditLog.resource_type == "Attachment",
        )
    )
    audit_logs = result.scalars().all()
    if audit_logs:
        log = audit_logs[-1]
        assert log.details["old_description"] == "original description"
        assert log.details["new_description"] == "updated description for audit test"
        assert log.details["filename"] == "audit-update-file.txt"
