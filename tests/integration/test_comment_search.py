"""Integration tests for comment search.

Covers:
- Core comment search: keyword and semantic modes find comments
- Result metadata: issue key in title, snippet contains matched text
- Visibility: comments inherit issue visibility (CTE-based)
- Configurable indexing: min length, bot exclusion, index toggle

NOTE: Comment search via FTS/semantic is not yet implemented.
Tests that require it are marked xfail.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.search import EmbeddingModel, SearchChunk, SearchSource
from specivo.models.user import User
from specivo.services.chunking_service import ChunkingService
from specivo.services.embedding_service import EmbeddingService
from specivo.services.journal_service import JournalService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, ServiceAccountFactory, UserFactory

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SEARCH_URL = "/api/v1/search/"


async def _make_user(db: AsyncSession, login: str = "cmtsrch_user") -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_service_account(db: AsyncSession, login: str = "cmtsrch_bot") -> User:
    user = ServiceAccountFactory.build(login=login, status="active")
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
    key: str = "CMT",
    identifier: str = "comment-search-project",
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


async def _add_member(
    db: AsyncSession,
    project: Project,
    user: User,
    issues_visibility: str = "all",
) -> None:
    role = Role(
        name=f"Member-{project.key}-{user.id}",
        permissions=["*"],
        builtin=0,
        issues_visibility=issues_visibility,
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
        name="cmt-test-mock",
        provider="mock",
        model_name="mock-1536",
        dimensions=1536,
        is_default=True,
    )
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return model


async def _create_issue_directly(
    db: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    user: User,
    subject: str,
    description: str | None = None,
    is_private: bool = False,
) -> Issue:
    """Create an issue directly in the DB (bypasses API triggers)."""
    # Get next sequence number
    from sqlalchemy import func

    result = await db.execute(
        select(func.coalesce(func.max(Issue.sequence_number), 0)).where(Issue.project_id == project.id)
    )
    next_seq = result.scalar_one() + 1

    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=next_seq,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject=subject,
        description=description,
        is_private=is_private,
    )
    db.add(issue)
    await db.commit()
    await db.refresh(issue)
    return issue


async def _add_comment(
    db: AsyncSession,
    issue: Issue,
    user: User,
    notes: str,
    api_key_id: int | None = None,
) -> Journal:
    """Add a comment to an issue via JournalService."""
    svc = JournalService()
    journal = await svc.add_comment(
        db,
        issue,
        user,
        notes,
        api_key_id=api_key_id,
    )
    await db.commit()
    await db.refresh(journal)
    return journal


async def _index_comment(
    db: AsyncSession,
    journal: Journal,
    issue: Issue,
    model_id: int | None = None,
) -> SearchSource | None:
    """Index a comment's text into the search system."""
    chunking = ChunkingService()
    chunks = chunking.chunk_journal(journal.notes)
    if not chunks:
        return None

    emb_svc = EmbeddingService()
    source = await emb_svc.embed_source(
        db,
        source_type="journal",
        entity_id=journal.id,
        project_id=issue.project_id,
        chunks=chunks,
        model_id=model_id,
    )
    await db.commit()
    return source


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
    return await _make_user(db_session, login="comment_search_user")


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
    """Client authenticated as a project member with mock embedding model."""
    await _add_member(db_session, project, search_user)
    token = await _login(client, search_user.login)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------------------------------------------------------------------------
# Core comment search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_comment_found_via_keyword_search(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """A comment's text is discoverable via FTS keyword search.

    After indexing, searching for words in the comment text must return
    a result with result_type='comment'.
    """
    tracker, status, priority = lookups
    issue = await _create_issue_directly(
        db_session,
        project,
        tracker,
        status,
        priority,
        search_user,
        "Issue for comment keyword search",
    )
    journal = await _add_comment(
        db_session,
        issue,
        search_user,
        "The crystallography analysis revealed unexpected lattice defects",
    )
    await _index_comment(db_session, journal, issue)

    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "crystallography lattice defects", "mode": "keyword"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    comment_results = [r for r in data["items"] if r["result_type"] == "comment"]
    assert len(comment_results) >= 1, "Comment text must be discoverable via keyword search"


@pytest.mark.asyncio
async def test_comment_found_via_semantic_search(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Semantic search accepts comment queries without error.

    Mock embeddings are hash-based and won't produce meaningful similarity,
    so we only verify the API returns a valid response — not that results
    are found. Real similarity matching requires a real embedding model.
    """
    tracker, status, priority = lookups
    issue = await _create_issue_directly(
        db_session,
        project,
        tracker,
        status,
        priority,
        search_user,
        "Issue for comment semantic search",
    )
    journal = await _add_comment(
        db_session,
        issue,
        search_user,
        "The mitochondrial membrane potential fluctuates during apoptosis signaling",
    )
    await _index_comment(db_session, journal, issue)

    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "mitochondrial membrane apoptosis", "mode": "semantic"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data["items"], list)


@pytest.mark.asyncio
async def test_comment_result_includes_issue_key(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Comment search result title must contain the parent issue key (e.g. CMT-1)."""
    tracker, status, priority = lookups
    issue = await _create_issue_directly(
        db_session,
        project,
        tracker,
        status,
        priority,
        search_user,
        "Issue for key display test",
    )
    journal = await _add_comment(
        db_session,
        issue,
        search_user,
        "Electrochemical impedance spectroscopy measurements completed",
    )
    await _index_comment(db_session, journal, issue)

    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "electrochemical impedance spectroscopy", "mode": "keyword"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    comment_results = [r for r in data["items"] if r["result_type"] == "comment"]
    assert len(comment_results) >= 1

    expected_key = f"{project.key}-{issue.sequence_number}"
    titles = [r["title"] for r in comment_results]
    assert any(expected_key in t for t in titles), (
        f"Comment result title must contain parent issue key '{expected_key}', got titles: {titles}"
    )


@pytest.mark.asyncio
async def test_comment_result_includes_snippet(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Comment search result snippet must contain the matched text."""
    tracker, status, priority = lookups
    issue = await _create_issue_directly(
        db_session,
        project,
        tracker,
        status,
        priority,
        search_user,
        "Issue for snippet test",
    )
    journal = await _add_comment(
        db_session,
        issue,
        search_user,
        "Superconducting qubit coherence time improved to 300 microseconds",
    )
    await _index_comment(db_session, journal, issue)

    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "superconducting qubit coherence", "mode": "keyword"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    comment_results = [r for r in data["items"] if r["result_type"] == "comment"]
    assert len(comment_results) >= 1

    snippets = [r["snippet"] for r in comment_results if r.get("snippet")]
    assert any("coherence" in s.lower() or "superconducting" in s.lower() for s in snippets), (
        f"Comment snippet must contain matched text, got snippets: {snippets}"
    )


# ---------------------------------------------------------------------------
# Visibility — comments inherit issue access control
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_comment_on_private_issue_not_visible(
    db_session: AsyncSession,
    client: AsyncClient,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Non-member cannot find a comment on a private project issue.

    A private project's comments must not be visible to users who are not
    members of that project.
    """
    tracker, status, priority = lookups

    # Create a private project
    private_proj = await _make_project(db_session, key="PRV", identifier="private-cmt-proj", is_public=False)

    # Owner creates issue + comment
    owner = await _make_user(db_session, login="prv_owner")
    await _add_member(db_session, private_proj, owner)

    issue = await _create_issue_directly(
        db_session,
        private_proj,
        tracker,
        status,
        priority,
        owner,
        "Private project issue",
    )
    journal = await _add_comment(
        db_session,
        issue,
        owner,
        "Ferroelectric domain wall dynamics under applied electric field",
    )
    await _index_comment(db_session, journal, issue)

    # Non-member searches for the comment text
    outsider = await _make_user(db_session, login="prv_outsider")
    token = await _login(client, outsider.login)
    client.headers["Authorization"] = f"Bearer {token}"

    resp = await client.get(
        SEARCH_URL,
        params={"q": "ferroelectric domain wall dynamics", "mode": "keyword"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    comment_results = [r for r in data["items"] if r["result_type"] == "comment"]
    assert len(comment_results) == 0, "Comments on private project issues must not be visible to non-members"


@pytest.mark.asyncio
async def test_comment_on_accessible_issue_visible(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """A project member can find comments on issues they have access to."""
    tracker, status, priority = lookups
    issue = await _create_issue_directly(
        db_session,
        project,
        tracker,
        status,
        priority,
        search_user,
        "Accessible issue for comment search",
    )
    journal = await _add_comment(
        db_session,
        issue,
        search_user,
        "Topological insulator surface states measured via ARPES technique",
    )
    await _index_comment(db_session, journal, issue)

    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "topological insulator ARPES", "mode": "keyword"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    comment_results = [r for r in data["items"] if r["result_type"] == "comment"]
    assert len(comment_results) >= 1, "Project members must be able to find comments on accessible issues"


@pytest.mark.asyncio
async def test_comment_inherits_issue_visibility(
    db_session: AsyncSession,
    client: AsyncClient,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Comment visibility follows the same CTE rules as issue search.

    A user with 'own' visibility can only see comments on issues they authored
    or are assigned to, not all issues in the project.
    """
    tracker, status, priority = lookups

    project = await _make_project(db_session, key="VIS", identifier="visibility-cmt-proj", is_public=False)

    # Create two users: author (creates the issue) and viewer (own-only visibility)
    author = await _make_user(db_session, login="vis_author")
    viewer = await _make_user(db_session, login="vis_viewer")

    await _add_member(db_session, project, author, issues_visibility="all")
    await _add_member(db_session, project, viewer, issues_visibility="own")

    # Author creates an issue (viewer is NOT author/assignee)
    issue = await _create_issue_directly(
        db_session,
        project,
        tracker,
        status,
        priority,
        author,
        "Author-only visible issue",
    )
    journal = await _add_comment(
        db_session,
        issue,
        author,
        "Magnetohydrodynamic turbulence simulation results available",
    )
    await _index_comment(db_session, journal, issue)

    # Viewer (own-only) searches — should NOT find this comment
    token = await _login(client, viewer.login)
    client.headers["Authorization"] = f"Bearer {token}"

    resp = await client.get(
        SEARCH_URL,
        params={"q": "magnetohydrodynamic turbulence", "mode": "keyword"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    comment_results = [r for r in data["items"] if r["result_type"] == "comment"]
    assert len(comment_results) == 0, (
        "Comments must inherit issue visibility — own-only viewer cannot see "
        "comments on issues they did not author and are not assigned to"
    )


# ---------------------------------------------------------------------------
# Configurable indexing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_comment_not_indexed(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Comments shorter than search_min_comment_length must not be indexed.

    Short comments like 'ok', 'thanks', '+1' add noise to search results.
    The setting search_min_comment_length (default: 20 chars) excludes them.
    """
    tracker, status, priority = lookups
    issue = await _create_issue_directly(
        db_session,
        project,
        tracker,
        status,
        priority,
        search_user,
        "Issue for short comment test",
    )

    # A very short comment — should NOT be indexed
    short_journal = await _add_comment(db_session, issue, search_user, "ok thanks")
    result = await _index_comment(db_session, short_journal, issue)

    # Verify: either index_comment returns None, or no SearchSource is created
    # for this journal's content
    sources = await db_session.execute(
        select(SearchSource).where(
            SearchSource.source_type == "journal",
            SearchSource.entity_id == short_journal.id,
        )
    )
    source = sources.scalar_one_or_none()
    assert source is None, "Short comments (below search_min_comment_length) must not be indexed"


@pytest.mark.asyncio
@pytest.mark.xfail(reason="Bot comment exclusion not yet enforced in chunking/embedding layer")
async def test_bot_comment_excluded_when_setting_enabled(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Service account comments must be excluded when search_exclude_bot_comments=True.

    Automated bots often generate verbose comments (CI reports, deploy logs)
    that should not pollute search results.
    """
    tracker, status, priority = lookups
    issue = await _create_issue_directly(
        db_session,
        project,
        tracker,
        status,
        priority,
        search_user,
        "Issue for bot comment exclusion test",
    )

    # Create a service account and its comment
    bot_user = await _make_service_account(db_session, login="ci_bot_cmt")
    await _add_member(db_session, project, bot_user)

    from specivo.services.api_key_service import ApiKeyService

    api_svc = ApiKeyService()
    api_key, _raw = await api_svc.create_key(db_session, bot_user.id, name="ci-key")
    await db_session.flush()

    bot_journal = await _add_comment(
        db_session,
        issue,
        bot_user,
        "Automated deployment pipeline completed with spectrophotometric calibration",
        api_key_id=api_key.id,
    )

    # With search_exclude_bot_comments=True, the comment should NOT be indexed
    # The indexer checks if the journal's user is a service account or
    # if it has an api_key_id set
    result = await _index_comment(db_session, bot_journal, issue)

    sources = await db_session.execute(
        select(SearchSource).where(
            SearchSource.source_type == "journal",
            SearchSource.entity_id == bot_journal.id,
        )
    )
    source = sources.scalar_one_or_none()
    assert source is None, (
        "Bot/service-account comments must be excluded from indexing when search_exclude_bot_comments is enabled"
    )


@pytest.mark.asyncio
@pytest.mark.xfail(reason="search_index_comments toggle not yet enforced in chunking/embedding layer")
async def test_comment_indexing_disabled_via_setting(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
    monkeypatch: pytest.MonkeyPatch,
):
    """When search_index_comments=False, no comments are indexed at all.

    This is a global kill switch for comment search.
    """
    from specivo.core.config import get_settings

    monkeypatch.setattr(get_settings(), "search_index_comments", False)

    tracker, status, priority = lookups
    issue = await _create_issue_directly(
        db_session,
        project,
        tracker,
        status,
        priority,
        search_user,
        "Issue for indexing disable test",
    )
    journal = await _add_comment(
        db_session,
        issue,
        search_user,
        "Gravitational lensing observations of distant quasar populations",
    )

    # With search_index_comments=False, indexing should be a no-op
    result = await _index_comment(db_session, journal, issue)

    sources = await db_session.execute(
        select(SearchSource).where(
            SearchSource.source_type == "journal",
            SearchSource.entity_id == journal.id,
        )
    )
    source = sources.scalar_one_or_none()
    assert source is None, "No comments should be indexed when search_index_comments=False"


@pytest.mark.asyncio
async def test_normal_comment_indexed_by_default(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """With default settings, a normal-length comment by a human user is indexed.

    This is the positive case: default config, normal comment, human user.
    """
    tracker, status, priority = lookups
    issue = await _create_issue_directly(
        db_session,
        project,
        tracker,
        status,
        priority,
        search_user,
        "Issue for default indexing test",
    )
    journal = await _add_comment(
        db_session,
        issue,
        search_user,
        "Nanoparticle self-assembly mechanisms observed under transmission electron microscopy",
    )
    source = await _index_comment(db_session, journal, issue)

    # With default settings, the comment should be indexed
    assert source is not None, "Normal comments must be indexed with default settings"

    # Verify chunks exist
    result = await db_session.execute(select(SearchChunk).where(SearchChunk.source_id == source.id))
    chunks = result.scalars().all()
    assert len(chunks) >= 1
    assert "nanoparticle" in chunks[0].content.lower()
