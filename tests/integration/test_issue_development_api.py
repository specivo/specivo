"""``GET /api/v1/issues/{ref}/development/`` and the issue page's Development tab: the work linked to an issue."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
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
        provider=integration.provider,
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


def _dev_tab(page: str) -> str | None:
    """The Development tab's link in the tab row, or None when there is no such tab."""
    match = re.search(r'<a [^>]*data-testid="development-tab"[^>]*>(.*?)</a>', page, re.S)
    return match.group(1) if match else None


async def test_tab_renders_pull_requests_and_commits(client, db_session, seeded):
    page = await _page(client, db_session, seeded["project"], "DEVA-1", "dev_page_member")
    assert 'data-testid="development-panel"' in page
    assert 'href="https://forge.example.com/example-org/widgets/pull/7"' in page
    assert "Add the login form" in page
    assert "1111111" in page and "0000000" in page
    assert "alex@example.com" not in page


async def test_tab_label_counts_pull_requests_and_commits(client, db_session, seeded):
    page = await _page(client, db_session, seeded["project"], "DEVA-1", "dev_page_count")
    label = _dev_tab(page)
    assert label is not None
    assert "Development" in label
    assert '<span class="tab-count">3</span>' in label  # one pull request and two commits


async def test_tabs_keep_their_order(client, db_session, seeded):
    page = await _page(client, db_session, seeded["project"], "DEVA-1", "dev_page_order")
    order = re.findall(r'data-tab="(\w+)"', page)
    assert order == ["activity", "development", "relations", "attachments"]  # no time permission here


async def test_branches_section_is_gone_but_commit_branches_link_to_the_forge(client, db_session, seeded):
    page = await _page(client, db_session, seeded["project"], "DEVA-1", "dev_page_branches")
    assert ">Branches<" not in page
    # Each commit's branch, the default branch too, links to the forge's branch page.
    assert 'href="https://forge.example.com/example-org/widgets/tree/feat/DEVA-1-login"' in page
    assert 'href="https://forge.example.com/example-org/widgets/tree/main"' in page


async def test_branch_url_stays_out_of_the_api(client, db_session, seeded):
    headers = await _headers(client, db_session, "dev_api_no_branch_url", seeded["project"])
    body = (await client.get("/api/v1/issues/DEVA-1/development/", headers=headers)).json()
    assert body["commits"] and all("branch_url" not in c for c in body["commits"])
    from specivo.schemas.repository import DevelopmentCommitOut

    assert "branch_url" not in DevelopmentCommitOut.model_json_schema(mode="serialization")["properties"]


async def test_commit_branch_is_plain_text_without_a_web_url(client, db_session, seeded):
    from specivo.models.repository import RepositoryIntegration

    integration = (await db_session.execute(select(RepositoryIntegration))).scalars().first()
    integration.repo_url = "git@forge.example.com:example-org/widgets.git"
    await db_session.commit()
    page = await _page(client, db_session, seeded["project"], "DEVA-1", "dev_page_ssh")
    assert "<code>feat/DEVA-1-login</code>" in page
    assert "/tree/feat/DEVA-1-login" not in page


async def test_no_tab_without_linked_work(client, db_session, seeded):
    page = await _page(client, db_session, seeded["project"], "DEVA-2", "dev_page_member2")
    assert _dev_tab(page) is None
    assert 'data-testid="development-panel"' not in page


async def test_a_branch_alone_shows_no_tab(client, db_session, seeded, monkeypatch):
    from specivo.schemas.repository import DevelopmentBranchOut, IssueDevelopmentOut
    from specivo.services import repository_service

    async def _only_a_branch(session, issue_id):
        branch = DevelopmentBranchOut(name="feat/x", url=None, repository="example-org/widgets", provider="github")
        return IssueDevelopmentOut(commit_count=0, commits=[], pull_requests=[], branches=[branch])

    monkeypatch.setattr(repository_service, "development_for_issue", _only_a_branch)
    page = await _page(client, db_session, seeded["project"], "DEVA-2", "dev_page_branch_only")
    assert _dev_tab(page) is None
    assert 'data-testid="development-panel"' not in page


async def test_tab_hidden_from_anonymous_visitors(client, db_session, seeded):
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
    assert _dev_tab(resp.text) is None
    assert 'data-testid="development-panel"' not in resp.text
    assert 'data-testid="copy-branch-name"' not in resp.text
    assert "1111111" not in resp.text
