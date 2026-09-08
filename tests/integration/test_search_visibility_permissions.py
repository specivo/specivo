"""Search visibility SQL: role permissions and group-held memberships.

``search_service`` builds access control as raw SQL so the visibility test can
live inside the same statement as the ranking and pagination. These tests pin
the two rules that SQL has to reproduce faithfully:

- a member is judged by their roles' ``permissions`` — ``view_issues`` gates
  issues, comments and issue attachments, ``view_wiki`` gates wiki pages and
  their attachments, and ``"*"`` grants both;
- a ``members`` row reaches a user directly *or* through a user group, so a
  user whose only link to a project is a group sees exactly what that group's
  roles grant.

Both query paths are covered: the CTE path (``user_visibility`` /
``public_projects``), which every live entry point uses, and the inline path
built by ``_issue_visibility_clause`` / ``_wiki_visibility_clause``, which
carries its own ``members`` joins. A parity helper asserts the two agree.
"""

from __future__ import annotations

import io
import itertools

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.search import EmbeddingModel
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.services.chunking_service import ChunkingService
from specivo.services.embedding_service import EmbeddingService
from specivo.services.journal_service import JournalService
from specivo.services.permission_service import Permission, clear_role_cache
from specivo.services.search_service import SearchService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

pytestmark = pytest.mark.integration

SEARCH_URL = "/api/v1/search/"

# Search terms unique to this module, so results cannot be borrowed from
# fixtures created by other tests.
TERM = "zephyrine gate"

_counter = itertools.count(1)

_service = SearchService()


@pytest.fixture(autouse=True)
def _reset_role_cache():
    """The role cache is a module global; keep it from leaking between tests."""
    clear_role_cache()
    yield
    clear_role_cache()


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession, login: str) -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_admin(db: AsyncSession, login: str) -> User:
    user = AdminUserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client: AsyncClient, login: str) -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _authenticate(client: AsyncClient, user: User) -> None:
    token = await _login(client, user.login)
    client.headers["Authorization"] = f"Bearer {token}"


async def _make_project(db: AsyncSession, key: str, identifier: str, *, is_public: bool) -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier, is_public=is_public)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


async def _seed_lookups(db: AsyncSession) -> tuple[Tracker, IssueStatus, IssuePriority]:
    status = StatusFactory.build(name="New", position=1, category="backlog")
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


async def _make_role(
    db: AsyncSession,
    name: str,
    permissions: list[str],
    issues_visibility: str = "default",
) -> Role:
    role = Role(
        name=f"{name}-{next(_counter)}",
        permissions=permissions,
        builtin=0,
        issues_visibility=issues_visibility,
    )
    db.add(role)
    await db.commit()
    await db.refresh(role)
    return role


async def _add_principal(
    db: AsyncSession,
    project: Project,
    role: Role,
    *,
    user: User | None = None,
    group: UserGroup | None = None,
) -> Member:
    """Add a user or a group to *project* holding *role*."""
    member = Member(
        user_id=user.id if user is not None else None,
        group_id=group.id if group is not None else None,
        project_id=project.id,
    )
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()
    return member


async def _make_group(db: AsyncSession, name: str, members: list[User]) -> UserGroup:
    group = UserGroup(name=f"{name}-{next(_counter)}")
    db.add(group)
    await db.flush()
    for user in members:
        db.add(UserGroupMember(group_id=group.id, user_id=user.id))
    await db.commit()
    await db.refresh(group)
    return group


async def _enable_wiki(db: AsyncSession, project: Project) -> None:
    db.add(EnabledModule(project_id=project.id, name="wiki"))
    await db.commit()


async def _create_mock_model(db: AsyncSession) -> EmbeddingModel:
    model = EmbeddingModel(
        name="visperm-mock",
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
    db: AsyncSession,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    author: User,
    subject: str,
    *,
    assigned_to: User | None = None,
    is_private: bool = False,
) -> Issue:
    """Create an issue directly in the DB; triggers maintain its tsvector."""
    tracker, status, priority = lookups
    result = await db.execute(
        select(func.coalesce(func.max(Issue.sequence_number), 0)).where(Issue.project_id == project.id)
    )
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=result.scalar_one() + 1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=author.id,
        assigned_to_id=assigned_to.id if assigned_to is not None else None,
        subject=subject,
        description=None,
        is_private=is_private,
    )
    db.add(issue)
    await db.commit()
    await db.refresh(issue)
    return issue


async def _add_indexed_comment(
    db: AsyncSession,
    issue: Issue,
    author: User,
    notes: str,
    model: EmbeddingModel,
) -> Journal:
    journal = await JournalService().add_comment(db, issue, author, notes)
    await db.commit()
    await db.refresh(journal)

    chunks = ChunkingService().chunk_journal(journal.notes)
    assert chunks, "comment is too short to be indexed"
    await EmbeddingService().embed_source(
        db,
        source_type="journal",
        entity_id=journal.id,
        project_id=issue.project_id,
        chunks=chunks,
        model_id=model.id,
    )
    await db.commit()
    return journal


async def _create_wiki_page(client: AsyncClient, project_key: str, title: str, body: str) -> dict:
    resp = await client.post(f"/api/v1/projects/{project_key}/wiki/", json={"title": title, "text": body})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _upload_attachment(
    client: AsyncClient,
    container_type: str,
    container_id: int,
    filename: str,
    description: str,
) -> dict:
    resp = await client.post(
        "/api/v1/attachments/",
        files={"file": (filename, io.BytesIO(b"attachment body text"), "text/plain")},
        data={
            "container_type": container_type,
            "container_id": str(container_id),
            "description": description,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _search(client: AsyncClient, scope: str, query: str = TERM) -> dict:
    resp = await client.get(SEARCH_URL, params={"q": query, "scope": scope})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _counts(client: AsyncClient, user: User) -> dict[str, int]:
    """Result counts per scope for *user*, via the live search endpoint (CTE path)."""
    await _authenticate(client, user)
    return {
        scope: (await _search(client, scope))["total_count"] for scope in ("issues", "wiki", "comments", "attachments")
    }


# ---------------------------------------------------------------------------
# Direct SQL helpers — exercise both visibility query paths
# ---------------------------------------------------------------------------


async def _visible_issue_ids(db: AsyncSession, user: User) -> set[int]:
    """Issue ids the visibility SQL admits for *user*, asserting path parity.

    Runs the inline clause (``_issue_visibility_clause``, its own ``members``
    joins) and the CTE clause (``_issue_visibility_cte_clause`` on top of
    ``_visibility_cte_sql``) and requires them to agree before returning.
    """
    params = {"current_user_id": user.id}

    inline_sql = f"SELECT i.id FROM issues i WHERE true {_service._issue_visibility_clause(user)}"
    inline = {row[0] for row in await db.execute(text(inline_sql), params)}

    cte_sql = (
        f"{_service._visibility_cte_sql(user)}\n"
        f"SELECT i.id FROM issues i WHERE true {_service._issue_visibility_cte_clause(user)}"
    )
    cte = {row[0] for row in await db.execute(text(cte_sql), params)}

    assert inline == cte, "inline and CTE issue visibility clauses disagree"
    return inline


async def _visible_wiki_project_ids(db: AsyncSession, user: User) -> set[int]:
    """Project ids whose wikis the visibility SQL admits for *user*, asserting parity."""
    params = {"current_user_id": user.id}

    inline_sql = f"SELECT w.project_id FROM wikis w WHERE true {_service._wiki_visibility_clause(user)}"
    inline = {row[0] for row in await db.execute(text(inline_sql), params)}

    cte_sql = (
        f"{_service._visibility_cte_sql(user)}\n"
        f"SELECT w.project_id FROM wikis w WHERE true {_service._wiki_visibility_cte_clause(user)}"
    )
    cte = {row[0] for row in await db.execute(text(cte_sql), params)}

    assert inline == cte, "inline and CTE wiki visibility clauses disagree"
    return inline


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def lookups(db_session: AsyncSession) -> tuple[Tracker, IssueStatus, IssuePriority]:
    return await _seed_lookups(db_session)


@pytest_asyncio.fixture
async def mock_model(db_session: AsyncSession) -> EmbeddingModel:
    return await _create_mock_model(db_session)


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    return await _make_admin(db_session, "visperm_admin")


@pytest_asyncio.fixture
async def member(db_session: AsyncSession) -> User:
    return await _make_user(db_session, "visperm_member")


@pytest_asyncio.fixture
async def outsider(db_session: AsyncSession) -> User:
    return await _make_user(db_session, "visperm_outsider")


class Content:
    """Searchable content of one project, all matching ``TERM``."""

    def __init__(self, project: Project, issue: Issue, wiki_page_id: int):
        self.project = project
        self.issue = issue
        self.wiki_page_id = wiki_page_id


async def _seed_content(
    db: AsyncSession,
    client: AsyncClient,
    project: Project,
    admin: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
    label: str,
) -> Content:
    """Populate *project* with an issue, a comment, a wiki page and attachments."""
    await _enable_wiki(db, project)
    issue = await _create_issue(db, project, lookups, admin, f"Zephyrine gate issue {label}")
    await _add_indexed_comment(db, issue, admin, f"Zephyrine gate comment about {label} handling", mock_model)

    await _authenticate(client, admin)
    page = await _create_wiki_page(
        client,
        project.key,
        f"Zephyrine Gate Handbook {label}",
        f"Zephyrine gate wiki body for {label}",
    )
    await _upload_attachment(client, "Issue", issue.id, f"issue-{label}.txt", f"Zephyrine gate issue file {label}")
    await _upload_attachment(client, "WikiPage", page["id"], f"wiki-{label}.txt", f"Zephyrine gate wiki file {label}")
    return Content(project, issue, page["id"])


@pytest_asyncio.fixture
async def private_content(
    db_session: AsyncSession,
    client: AsyncClient,
    admin: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
) -> Content:
    project = await _make_project(db_session, key="VPP", identifier="visperm-private", is_public=False)
    return await _seed_content(db_session, client, project, admin, lookups, mock_model, "private")


@pytest_asyncio.fixture
async def public_content(
    db_session: AsyncSession,
    client: AsyncClient,
    admin: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
) -> Content:
    project = await _make_project(db_session, key="VPU", identifier="visperm-public", is_public=True)
    return await _seed_content(db_session, client, project, admin, lookups, mock_model, "public")


# ---------------------------------------------------------------------------
# Permission gate — roles.permissions decides, not bare membership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_role_without_view_issues_hides_issues_comments_and_attachments(
    db_session: AsyncSession,
    client: AsyncClient,
    private_content: Content,
    member: User,
):
    """A member whose role omits view_issues gets no issue, comment or attachment."""
    role = await _make_role(db_session, "NoIssues", [Permission.VIEW_WIKI, Permission.ADD_ISSUES])
    await _add_principal(db_session, private_content.project, role, user=member)

    counts = await _counts(client, member)

    assert counts["issues"] == 0
    assert counts["comments"] == 0
    # The wiki attachment is still reachable — only the issue attachment is gated.
    assert counts["attachments"] == 1
    assert counts["wiki"] == 1


@pytest.mark.asyncio
async def test_role_without_view_wiki_hides_wiki_pages_and_attachments(
    db_session: AsyncSession,
    client: AsyncClient,
    private_content: Content,
    member: User,
):
    """A member whose role omits view_wiki gets no wiki page and no wiki attachment."""
    role = await _make_role(db_session, "NoWiki", [Permission.VIEW_ISSUES])
    await _add_principal(db_session, private_content.project, role, user=member)

    counts = await _counts(client, member)

    assert counts["wiki"] == 0
    assert counts["issues"] == 1
    assert counts["comments"] == 1
    # Only the issue attachment survives.
    assert counts["attachments"] == 1


@pytest.mark.asyncio
async def test_reporter_style_role_sees_issues_and_wiki(
    db_session: AsyncSession,
    client: AsyncClient,
    private_content: Content,
    member: User,
):
    """A role granting both view permissions still sees everything it should."""
    role = await _make_role(
        db_session,
        "Reporter",
        [Permission.VIEW_ISSUES, Permission.VIEW_WIKI, Permission.ADD_ISSUES],
    )
    await _add_principal(db_session, private_content.project, role, user=member)

    counts = await _counts(client, member)

    assert counts == {"issues": 1, "wiki": 1, "comments": 1, "attachments": 2}


@pytest.mark.asyncio
async def test_wildcard_role_sees_everything(
    db_session: AsyncSession,
    client: AsyncClient,
    private_content: Content,
    member: User,
):
    """The ``["*"]`` wildcard keeps granting every permission."""
    role = await _make_role(db_session, "Manager", ["*"])
    await _add_principal(db_session, private_content.project, role, user=member)

    counts = await _counts(client, member)

    assert counts == {"issues": 1, "wiki": 1, "comments": 1, "attachments": 2}


@pytest.mark.asyncio
async def test_view_issues_with_own_visibility_sees_only_authored_or_assigned(
    db_session: AsyncSession,
    client: AsyncClient,
    private_content: Content,
    admin: User,
    member: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """view_issues plus issues_visibility='own' still narrows to authored/assigned."""
    role = await _make_role(db_session, "OwnOnly", [Permission.VIEW_ISSUES], issues_visibility="own")
    await _add_principal(db_session, private_content.project, role, user=member)

    authored = await _create_issue(
        db_session, private_content.project, lookups, member, "Zephyrine gate issue authored"
    )
    assigned = await _create_issue(
        db_session,
        private_content.project,
        lookups,
        admin,
        "Zephyrine gate issue assigned",
        assigned_to=member,
    )

    assert await _visible_issue_ids(db_session, member) == {authored.id, assigned.id}

    await _authenticate(client, member)
    data = await _search(client, "issues")
    assert data["total_count"] == 2


# ---------------------------------------------------------------------------
# Group-held memberships
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_membership_grants_access_to_private_project(
    db_session: AsyncSession,
    client: AsyncClient,
    private_content: Content,
    member: User,
):
    """A user whose only link to the project is a group sees all of its content."""
    role = await _make_role(db_session, "GroupManager", ["*"])
    group = await _make_group(db_session, "Gatekeepers", [member])
    await _add_principal(db_session, private_content.project, role, group=group)

    assert await _visible_issue_ids(db_session, member) == {private_content.issue.id}
    assert private_content.project.id in await _visible_wiki_project_ids(db_session, member)

    counts = await _counts(client, member)
    assert counts == {"issues": 1, "wiki": 1, "comments": 1, "attachments": 2}


@pytest.mark.asyncio
async def test_group_role_without_view_wiki_hides_wiki(
    db_session: AsyncSession,
    client: AsyncClient,
    private_content: Content,
    member: User,
):
    """Roles come from the group, so the group's missing permission binds too."""
    role = await _make_role(db_session, "GroupNoWiki", [Permission.VIEW_ISSUES])
    group = await _make_group(db_session, "IssueReaders", [member])
    await _add_principal(db_session, private_content.project, role, group=group)

    assert await _visible_wiki_project_ids(db_session, member) == set()

    counts = await _counts(client, member)
    assert counts["issues"] == 1
    assert counts["wiki"] == 0
    assert counts["attachments"] == 1


@pytest.mark.asyncio
async def test_user_outside_the_group_sees_nothing(
    db_session: AsyncSession,
    client: AsyncClient,
    private_content: Content,
    member: User,
    outsider: User,
):
    """Only members of the group inherit its project access."""
    role = await _make_role(db_session, "GroupManager", ["*"])
    group = await _make_group(db_session, "Gatekeepers", [member])
    await _add_principal(db_session, private_content.project, role, group=group)

    assert await _visible_issue_ids(db_session, outsider) == set()
    assert await _visible_wiki_project_ids(db_session, outsider) == set()

    counts = await _counts(client, outsider)
    assert counts == {"issues": 0, "wiki": 0, "comments": 0, "attachments": 0}


@pytest.mark.asyncio
async def test_direct_and_group_memberships_union(
    db_session: AsyncSession,
    client: AsyncClient,
    private_content: Content,
    member: User,
):
    """Holding both kinds of membership grants the union — the wider role wins."""
    narrow = await _make_role(db_session, "DirectOwn", [Permission.VIEW_ISSUES], issues_visibility="own")
    await _add_principal(db_session, private_content.project, narrow, user=member)

    # Nothing yet: the issue was authored by someone else.
    assert await _visible_issue_ids(db_session, member) == set()

    wide = await _make_role(db_session, "GroupAll", [Permission.VIEW_ISSUES, Permission.VIEW_WIKI])
    group = await _make_group(db_session, "Widened", [member])
    await _add_principal(db_session, private_content.project, wide, group=group)

    assert await _visible_issue_ids(db_session, member) == {private_content.issue.id}
    assert private_content.project.id in await _visible_wiki_project_ids(db_session, member)

    counts = await _counts(client, member)
    assert counts["issues"] == 1
    assert counts["wiki"] == 1


# ---------------------------------------------------------------------------
# Preserved behaviour of the non-member / public-project branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_level_zero_member_does_not_fall_through_to_public_branch(
    db_session: AsyncSession,
    client: AsyncClient,
    public_content: Content,
    member: User,
    outsider: User,
):
    """A member whose roles grant no view_issues sees less than a passing stranger.

    The non-member branch is a fallback for people with no membership row at
    all. A member is judged by their roles instead, even when that leaves them
    with nothing — do not "fix" this into a fallback.
    """
    role = await _make_role(db_session, "PublicNoIssues", [Permission.VIEW_WIKI])
    await _add_principal(db_session, public_content.project, role, user=member)

    assert await _visible_issue_ids(db_session, member) == set()
    # The non-member branch itself is untouched: a stranger still sees the issue.
    assert await _visible_issue_ids(db_session, outsider) == {public_content.issue.id}

    assert (await _counts(client, member))["issues"] == 0
    assert (await _counts(client, outsider))["issues"] == 1


@pytest.mark.asyncio
async def test_public_project_wiki_stays_readable_without_view_wiki(
    db_session: AsyncSession,
    client: AsyncClient,
    public_content: Content,
    member: User,
    outsider: User,
):
    """The public-project wiki branch is unchanged: it has no membership guard.

    A member of a public project who lacks ``view_wiki`` still reaches its wiki
    through the public branch, exactly as before. Whether search should be that
    permissive about public projects is a separate question about builtin
    non-member roles; this test pins today's answer so a refactor cannot move
    it by accident.
    """
    role = await _make_role(db_session, "PublicNoWiki", [Permission.VIEW_ISSUES])
    await _add_principal(db_session, public_content.project, role, user=member)

    assert public_content.project.id in await _visible_wiki_project_ids(db_session, member)
    assert public_content.project.id in await _visible_wiki_project_ids(db_session, outsider)

    assert (await _counts(client, member))["wiki"] == 1


@pytest.mark.asyncio
async def test_private_project_stays_invisible_to_non_members(
    db_session: AsyncSession,
    client: AsyncClient,
    private_content: Content,
    outsider: User,
):
    """No membership and no public project means no results, on either path."""
    assert await _visible_issue_ids(db_session, outsider) == set()
    assert await _visible_wiki_project_ids(db_session, outsider) == set()

    counts = await _counts(client, outsider)
    assert counts == {"issues": 0, "wiki": 0, "comments": 0, "attachments": 0}


# ---------------------------------------------------------------------------
# Inline (non-CTE) clauses in isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_clauses_enforce_permissions_and_groups(
    db_session: AsyncSession,
    private_content: Content,
    member: User,
):
    """The inline builders apply the same gate as the CTE ones.

    ``_visible_issue_ids`` / ``_visible_wiki_project_ids`` assert parity, so
    this walks a role through three shapes and checks both paths each time.
    """
    # 1. A role granting neither view permission: a member, but sees nothing.
    role = await _make_role(db_session, "InlineNothing", [Permission.ADD_ISSUES])
    group = await _make_group(db_session, "InlineGroup", [member])
    await _add_principal(db_session, private_content.project, role, group=group)

    assert await _visible_issue_ids(db_session, member) == set()
    assert await _visible_wiki_project_ids(db_session, member) == set()

    # 2. Granting view_issues opens the issues only.
    role.permissions = [Permission.ADD_ISSUES, Permission.VIEW_ISSUES]
    await db_session.commit()

    assert await _visible_issue_ids(db_session, member) == {private_content.issue.id}
    assert await _visible_wiki_project_ids(db_session, member) == set()

    # 3. The wildcard opens the wiki too.
    role.permissions = ["*"]
    await db_session.commit()

    assert await _visible_issue_ids(db_session, member) == {private_content.issue.id}
    assert await _visible_wiki_project_ids(db_session, member) == {private_content.project.id}


@pytest.mark.asyncio
async def test_admin_clauses_are_empty(admin: User):
    """Admins bypass every clause, so the builders return nothing to append."""
    assert _service._issue_visibility_clause(admin) == ""
    assert _service._wiki_visibility_clause(admin) == ""
    assert _service._visibility_cte_sql(admin) == ""
    assert _service._issue_visibility_cte_clause(admin) == ""
    assert _service._wiki_visibility_cte_clause(admin) == ""
    assert _service._attachment_visibility_cte_clause(admin) == ""
