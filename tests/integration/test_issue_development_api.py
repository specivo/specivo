"""``GET /api/v1/issues/{ref}/development/``: the commits, pull requests and branches linked to an issue."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member, MemberRole
from specivo.models.repository import IssueCommitLink, IssuePullRequestLink, PullRequest, RepositoryCommit
from specivo.models.role import Role
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory
from tests.fixtures.forge_hooks import make_integration

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest_asyncio.fixture
async def seeded(db_session: AsyncSession):
    tracker, status, priority = TrackerFactory.build(), StatusFactory.build(), PriorityFactory.build()
    author = UserFactory.build(login="dev_api_author", status="active")
    project = ProjectFactory.build(key="DEVA", identifier="deva", name="Development API", is_public=False)
    db_session.add_all([tracker, status, priority, author, project])
    await db_session.flush()
    issues = []
    for n in (1, 2):
        issue = IssueFactory.build(
            project_id=project.id,
            project_key=project.key,
            sequence_number=n,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
            author_id=author.id,
        )
        db_session.add(issue)
        issues.append(issue)
    await db_session.flush()
    integration = await make_integration(db_session, project)
    commits = []
    for i, (branch, when) in enumerate(
        [("feat/DEVA-1-login", datetime(2026, 10, 1, tzinfo=UTC)), ("main", datetime(2026, 10, 2, tzinfo=UTC))]
    ):
        c = RepositoryCommit(
            integration_id=integration.id,
            sha=f"{i}" * 40,
            message=f"Commit {i}\n\nrefs DEVA-1",
            url=f"https://forge.example.com/example-org/widgets/commit/{i * 40}",
            author_name="Alex Example",
            author_email="alex@example.com",
            committed_at=when,
            branch=branch,
        )
        db_session.add(c)
        commits.append(c)
    pr = PullRequest(
        integration_id=integration.id,
        number=7,
        title="Add the login form",
        url="https://forge.example.com/example-org/widgets/pull/7",
        state="open",
        source_branch="feat/DEVA-1-login",
        target_branch="main",
        author_login="alex-example",
    )
    db_session.add(pr)
    await db_session.flush()
    for c in commits:
        db_session.add(IssueCommitLink(commit_id=c.id, issue_id=issues[0].id, source="message", keyword="refs"))
    db_session.add(IssuePullRequestLink(pull_request_id=pr.id, issue_id=issues[0].id, source="body", closing=True))
    await db_session.commit()
    return {"project": project, "issues": issues}


async def _headers(client: AsyncClient, db: AsyncSession, login: str, project=None) -> dict[str, str]:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.flush()
    if project is not None:
        role = Role(name=f"devapi-{login}", permissions=["view_issues"])
        db.add(role)
        await db.flush()
        member = Member(user_id=user.id, project_id=project.id)
        db.add(member)
        await db.flush()
        db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_non_member_of_private_project_gets_404(client, db_session, seeded):
    headers = await _headers(client, db_session, "dev_api_outsider")
    resp = await client.get("/api/v1/issues/DEVA-1/development/", headers=headers)
    assert resp.status_code == 404


async def test_anonymous_gets_401(client, seeded):
    resp = await client.get("/api/v1/issues/DEVA-1/development/")
    assert resp.status_code == 401


async def test_member_sees_commits_pull_requests_and_branches(client, db_session, seeded):
    headers = await _headers(client, db_session, "dev_api_member", seeded["project"])
    resp = await client.get("/api/v1/issues/DEVA-1/development/", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["commit_count"] == 2
    assert [c["short_sha"] for c in body["commits"]] == ["1111111", "0000000"]  # newest first
    first = body["commits"][0]
    assert first["first_line"] == "Commit 1"
    assert first["repository"] == "example-org/widgets"
    assert "author_email" not in first
    (pr,) = body["pull_requests"]
    assert (pr["number"], pr["state"], pr["closing"], pr["provider"]) == (7, "open", True, "github")
    assert pr["url"] == "https://forge.example.com/example-org/widgets/pull/7"
    # The default branch is not a development branch.
    assert [(b["name"], b["url"]) for b in body["branches"]] == [
        ("feat/DEVA-1-login", "https://forge.example.com/example-org/widgets/tree/feat/DEVA-1-login")
    ]


async def test_issue_without_development_is_empty(client, db_session, seeded):
    headers = await _headers(client, db_session, "dev_api_member2", seeded["project"])
    resp = await client.get("/api/v1/issues/DEVA-2/development/", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"commit_count": 0, "commits": [], "pull_requests": [], "branches": []}


async def _page(client: AsyncClient, db: AsyncSession, project, ref: str, login: str) -> str:
    headers = await _headers(client, db, login, project)
    token = headers["Authorization"].removeprefix("Bearer ")
    resp = await client.get(f"/issue/{ref}/", cookies={"access_token": token})
    assert resp.status_code == 200, resp.text
    return resp.text


async def test_panel_renders_pull_requests_branches_and_commits(client, db_session, seeded):
    page = await _page(client, db_session, seeded["project"], "DEVA-1", "dev_page_member")
    assert 'data-testid="development-panel"' in page
    assert 'href="https://forge.example.com/example-org/widgets/pull/7"' in page
    assert "Add the login form" in page
    assert "feat/DEVA-1-login" in page
    assert "1111111" in page and "0000000" in page
    assert "alex@example.com" not in page


async def test_empty_panel_is_hidden(client, db_session, seeded):
    page = await _page(client, db_session, seeded["project"], "DEVA-2", "dev_page_member2")
    assert 'data-testid="development-panel"' not in page


async def test_panel_hidden_from_anonymous_visitors(client, db_session, seeded):
    from specivo.services.anonymous_access_service import set_anonymous_access_enabled
    from tests.factories.user import AdminUserFactory

    admin = AdminUserFactory.build(login="dev_api_admin", status="active")
    db_session.add(admin)
    project = seeded["project"]
    project.is_public = True
    project.anonymous_permissions = ["view_issues"]
    await db_session.commit()
    await set_anonymous_access_enabled(db_session, True, admin, confirmed_projects=[project.key])
    await db_session.commit()

    resp = await client.get("/issue/DEVA-1/")
    assert resp.status_code == 200, resp.text
    assert "DEVA-1" in resp.text
    assert 'data-testid="development-panel"' not in resp.text
    assert 'data-testid="copy-branch-name"' not in resp.text
    assert "1111111" not in resp.text
