"""The issue page's Development tab: summary, timeline, its partials, and the detach/move controls."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule
from specivo.models.repository import (
    CiRun,
    Deployment,
    IssueCommitLink,
    IssuePullRequestLink,
    IssueReleaseLink,
    PullRequest,
    PullRequestReview,
    RepositoryCommit,
    RepositoryRelease,
)
from specivo.models.role import Role
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory
from tests.factories.version import VersionFactory
from tests.fixtures.forge_hooks import make_integration

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

NOW = datetime.now(UTC)
SHA = "a1" * 20
REPO_URL = "https://forge.example.com/example-org/widgets"


async def _member(db: AsyncSession, login: str, project, permissions: list[str]) -> None:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.flush()
    role = Role(name=f"devtab-{login}", permissions=permissions, issues_visibility="all")
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))


@pytest_asyncio.fixture
async def seeded(db_session: AsyncSession):
    """DTAB-1 with a commit, a pull request, a review, a failing build, two deployments and a release."""
    tracker, status, priority = TrackerFactory.build(), StatusFactory.build(), PriorityFactory.build()
    author = UserFactory.build(login="devtab_author", status="active")
    project = ProjectFactory.build(key="DTAB", identifier="dtab", name="Development tab", is_public=False)
    db_session.add_all([tracker, status, priority, author, project])
    await db_session.flush()
    db_session.add(EnabledModule(project_id=project.id, name="issue_tracking"))
    issues = []
    for n in (1, 2, 3):
        issue = IssueFactory.build(
            project_id=project.id,
            project_key=project.key,
            sequence_number=n,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
            author_id=author.id,
            subject=f"Tab issue {n}",
        )
        db_session.add(issue)
        issues.append(issue)
    await db_session.flush()
    integration = await make_integration(db_session, project)
    integration.repo_url = REPO_URL
    integration.repo_full_name = "example-org/widgets"
    version = VersionFactory.build(project_id=project.id, name="1.4")
    db_session.add(version)
    commit = RepositoryCommit(
        integration_id=integration.id,
        sha=SHA,
        message="Wire the login form\n\nrefs DTAB-1",
        url=f"{REPO_URL}/commit/{SHA}",
        author_name="Alex Example",
        author_email="alex@example.com",
        committed_at=NOW - timedelta(days=3),
        branch="feat/login",
    )
    pr = PullRequest(
        integration_id=integration.id,
        provider=integration.provider,
        number=7,
        title="Add the login form",
        url=f"{REPO_URL}/pull/7",
        state="open",
        source_branch="feat/login",
        target_branch="main",
        author_login="alex-example",
        provider_updated_at=NOW - timedelta(days=2),
    )
    db_session.add_all([commit, pr])
    await db_session.flush()
    db_session.add(IssueCommitLink(commit_id=commit.id, issue_id=issues[0].id, source="message", keyword="refs"))
    db_session.add(IssuePullRequestLink(pull_request_id=pr.id, issue_id=issues[0].id, source="body", closing=True))
    db_session.add(
        PullRequestReview(
            pull_request_id=pr.id,
            reviewer_login="sam-example",
            state="changes_requested",
            url=f"{REPO_URL}/pull/7#review",
            submitted_at=NOW - timedelta(days=1, hours=12),
        )
    )
    db_session.add(
        CiRun(
            integration_id=integration.id,
            provider="github",
            run_type="workflow_run",
            external_id=901,
            name="Tests",
            status="failure",
            head_sha=SHA,
            branch="feat/login",
            url=f"{REPO_URL}/actions/runs/901",
            attempt=1,
            occurred_at=NOW - timedelta(days=1),
        )
    )
    for ext, env, hours in ((1, "production-eu", 2), (2, "staging", 5)):
        db_session.add(
            Deployment(
                integration_id=integration.id,
                provider="github",
                external_id=ext,
                environment=env,
                status="success",
                sha=SHA,
                ref="main",
                url=f"{REPO_URL}/deployments/{env}",
                deployed_at=NOW - timedelta(hours=hours),
                occurred_at=NOW - timedelta(hours=hours),
            )
        )
    release = RepositoryRelease(
        integration_id=integration.id,
        provider="github",
        tag_name="v1.4",
        name="1.4",
        sha=SHA,
        url=f"{REPO_URL}/releases/tag/v1.4",
        is_release=True,
        released_at=NOW - timedelta(hours=1),
        occurred_at=NOW - timedelta(hours=1),
    )
    db_session.add(release)
    await db_session.flush()
    db_session.add(
        IssueReleaseLink(release_id=release.id, issue_id=issues[0].id, source="tag_commit", version_id=version.id)
    )
    await _member(db_session, "devtab_editor", project, ["view_issues", "edit_issues"])
    await _member(db_session, "devtab_viewer", project, ["view_issues"])
    await db_session.commit()
    return {
        "project": project,
        "issues": issues,
        "commit": commit,
        "pr": pr,
        "version": version,
        "integration": integration,
    }


async def _cookies(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"access_token": resp.json()["access_token"]}


async def _get(client: AsyncClient, url: str, login: str, **params) -> str:
    resp = await client.get(url, params=params, cookies=await _cookies(client, login))
    assert resp.status_code == 200, resp.text
    return resp.text


def _panel(page: str) -> str:
    start = page.index('data-testid="development-panel"')
    return page[start : page.index("</section>", start)]


class TestSummary:
    async def test_counts_and_combined_statuses(self, client, db_session, seeded):
        panel = _panel(await _get(client, "/issue/DTAB-1/", "devtab_viewer"))
        stat = {
            k: re.search(rf'data-testid="development-stat-{k}">(.*?)</li>', panel, re.S)
            for k in ("pull_request", "review", "ci", "deployment", "commit")
        }
        assert all(stat.values()), stat
        assert "1 pull request" in stat["pull_request"].group(1) and 'data-state="open"' in stat["pull_request"].group(
            1
        )
        assert "1 review" in stat["review"].group(1) and "Changes requested" in stat["review"].group(1)
        assert "1 build" in stat["ci"].group(1) and "Failing" in stat["ci"].group(1)
        assert "2 deployments" in stat["deployment"].group(1)
        assert "1 commit" in stat["commit"].group(1)
        assert "alex@example.com" not in panel

    async def test_latest_deployment_per_environment_type(self, client, db_session, seeded):
        panel = _panel(await _get(client, "/issue/DTAB-1/", "devtab_viewer"))
        deployed = re.findall(
            r'data-testid="development-deployed" data-environment-type="(\w*)">(.*?)</li>', panel, re.S
        )
        assert [t for t, _ in deployed] == ["production", "staging"]
        production = deployed[0][1]
        assert "Deployed to production" in production
        assert "2 hrs ago" in production
        # The absolute time is in the tooltip; the environment's own name links to the forge.
        assert re.search(r'<time class="sp-dev-time" datetime="[^"]+" title="[^"]+">2 hrs ago</time>', production)
        assert f'href="{REPO_URL}/deployments/production-eu"' in production

    async def test_released_in_links_the_version(self, client, db_session, seeded):
        panel = _panel(await _get(client, "/issue/DTAB-1/", "devtab_viewer"))
        released = re.search(r'data-testid="development-released">(.*?)</li>', panel, re.S).group(1)
        assert "Released in" in released
        version = seeded["version"]
        assert f'href="/projects/DTAB/versions/{version.id}/"' in released
        assert f'href="{REPO_URL}/releases/tag/v1.4"' in released

    async def test_repository_links_to_the_forge(self, client, db_session, seeded):
        panel = _panel(await _get(client, "/issue/DTAB-1/", "devtab_viewer"))
        assert f'href="{REPO_URL}"' in panel

    async def test_empty_parts_are_hidden(self, client, db_session, seeded):
        # DTAB-2 has only a commit: no pull requests, reviews, builds, deployments or releases.
        commit = RepositoryCommit(
            integration_id=seeded["integration"].id,
            sha="b2" * 20,
            message="Tidy DTAB-2",
            url=f"{REPO_URL}/commit/{'b2' * 20}",
            author_name="Alex Example",
            author_email="alex@example.com",
            committed_at=NOW,
            branch="main",
        )
        db_session.add(commit)
        await db_session.flush()
        db_session.add(IssueCommitLink(commit_id=commit.id, issue_id=seeded["issues"][1].id, source="message"))
        await db_session.commit()
        panel = _panel(await _get(client, "/issue/DTAB-2/", "devtab_viewer"))
        assert 'data-testid="development-stat-commit"' in panel
        for hidden in ("stat-pull_request", "stat-review", "stat-ci", "stat-deployment", "deployed", "released"):
            assert f'data-testid="development-{hidden}"' not in panel
        # One kind only: no filter.
        assert 'data-testid="development-filters"' not in panel


class TestTimeline:
    async def test_all_kinds_newest_first(self, client, db_session, seeded):
        panel = _panel(await _get(client, "/issue/DTAB-1/", "devtab_viewer"))
        kinds = re.findall(r'data-testid="development-item" data-kind="(\w+)"', panel)
        assert kinds == ["release", "deployment", "deployment", "ci", "review", "pull_request", "commit"]
        assert 'data-kind="pull_request"' in panel and "fixes" in panel
        assert f'href="{REPO_URL}/tree/feat/login"' in panel

    async def test_filter_partial(self, client, db_session, seeded):
        html = await _get(
            client, "/partials/issues/DTAB-1/development/timeline/", "devtab_viewer", kinds=["ci", "deployment"]
        )
        assert re.findall(r'data-testid="development-item" data-kind="(\w+)"', html) == [
            "deployment",
            "deployment",
            "ci",
        ]
        assert 'id="issue-dev-timeline"' in html
        # The selected chips are pressed; unknown kinds are ignored.
        assert re.search(r'aria-pressed="true"[^>]*data-kind="ci"', html, re.S)
        assert re.search(r'aria-pressed="false"[^>]*data-kind="commit"', html, re.S)

    async def test_show_more_pages(self, client, db_session, seeded, monkeypatch):
        monkeypatch.setattr("specivo.web.issue_development.DEVELOPMENT_TAB_PAGE_SIZE", 3)
        panel = _panel(await _get(client, "/issue/DTAB-1/", "devtab_viewer"))
        assert len(re.findall(r'data-testid="development-item"', panel)) == 3
        more = re.search(r'data-testid="development-more"\s+hx-get="([^"]+)"', panel)
        assert more and "offset=3" in more.group(1)
        assert "Show 3 more" in panel
        html = await _get(client, "/partials/issues/DTAB-1/development/timeline/", "devtab_viewer", offset=6)
        assert re.findall(r'data-kind="(\w+)"', html) == ["commit"]
        assert 'data-testid="development-more"' not in html
        assert 'id="issue-dev-timeline"' not in html

    async def test_refresh_partial_updates_the_tab_count(self, client, db_session, seeded):
        html = await _get(client, "/partials/issues/DTAB-1/development/", "devtab_viewer")
        assert 'data-testid="development-panel"' in html
        assert re.search(r'<span class="tab-count" id="development-tab-count" hx-swap-oob="true">2</span>', html)


class TestControls:
    async def test_editor_gets_detach_and_move_on_commits_and_pull_requests(self, client, db_session, seeded):
        panel = _panel(await _get(client, "/issue/DTAB-1/", "devtab_editor"))
        menus = re.findall(r'data-dev-kind="(\w+)" data-dev-id="(\d+)" data-dev-label="([^"]+)"', panel)
        assert sorted(menus) == sorted(
            [("commit", str(seeded["commit"].id), SHA[:7]), ("pull_request", str(seeded["pr"].id), "#7")]
        )
        assert panel.count('data-testid="development-detach"') == 2
        assert panel.count('data-testid="development-move"') == 2

    async def test_viewer_gets_no_controls(self, client, db_session, seeded):
        page = await _get(client, "/issue/DTAB-1/", "devtab_viewer")
        assert 'data-testid="development-panel"' in page
        assert "data-dev-id" not in page
        assert 'data-testid="development-detach"' not in page
        assert 'data-testid="development-move-dialog"' not in page

    async def test_partials_follow_the_same_rule(self, client, db_session, seeded):
        editor = await _get(client, "/partials/issues/DTAB-1/development/", "devtab_editor")
        viewer = await _get(client, "/partials/issues/DTAB-1/development/", "devtab_viewer")
        assert "data-dev-id" in editor and "data-dev-id" not in viewer

    async def test_anonymous_sees_nothing(self, client, db_session, seeded):
        from specivo.services.anonymous_access_service import set_anonymous_access_enabled
        from tests.factories.user import AdminUserFactory

        admin = AdminUserFactory.build(login="devtab_admin", status="active")
        db_session.add(admin)
        project = seeded["project"]
        project.is_public = True
        project.anonymous_permissions = ["view_issues"]
        await db_session.commit()
        await set_anonymous_access_enabled(db_session, True, admin, confirmed_projects=[project.key])
        await db_session.commit()

        page = await client.get("/issue/DTAB-1/")
        assert page.status_code == 200, page.text
        assert 'data-testid="development-panel"' not in page.text
        assert "data-dev-id" not in page.text
        for url in ("/partials/issues/DTAB-1/development/", "/partials/issues/DTAB-1/development/timeline/"):
            resp = await client.get(url)
            assert resp.status_code == 302
            assert resp.headers["location"] == "/login/"

    async def test_outsider_gets_404_from_the_partials(self, client, db_session, seeded):
        db_session.add(UserFactory.build(login="devtab_outsider", status="active"))
        await db_session.commit()
        cookies = await _cookies(client, "devtab_outsider")
        for url in ("/partials/issues/DTAB-1/development/", "/partials/issues/DTAB-1/development/timeline/"):
            assert (await client.get(url, cookies=cookies)).status_code == 404
