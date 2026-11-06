"""Development activity end to end: CI, deployments, tags and releases, reviews, detach/move, read access.

Celery is not running here: the endpoint's queueing is replaced by a recorder
and the tests run ``process_delivery`` on the test session themselves.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError, PermissionDeniedError
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.repository import (
    CiRun,
    Deployment,
    DevelopmentDetachment,
    IssueCommitLink,
    IssuePullRequestLink,
    IssueReleaseLink,
    PullRequestReview,
    RepositoryDelivery,
    RepositoryRelease,
)
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.repository_hooks import process_delivery
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory
from tests.factories.version import VersionFactory
from tests.fixtures import forge_activity as fa
from tests.fixtures.forge_hooks import (
    commit,
    hook_path,
    make_integration,
    pull_request_payload,
    push_payload,
    signed_headers,
)

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

SHA_C = "c" * 40
SHA_D = "d" * 40


@dataclass
class World:
    project: Project
    other: Project
    issues: dict[str, Issue]
    editor: User
    viewer: User
    outsider: User


async def _issue(db: AsyncSession, project: Project, number: int, author: User, lookups) -> Issue:
    tracker, status, priority = lookups
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=number,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=author.id,
        subject=f"Issue {number}",
    )
    db.add(issue)
    await db.flush()
    return issue


async def _member(db: AsyncSession, user: User, project: Project, permissions: list[str]) -> None:
    role = Role(name=f"devact-{user.login}-{project.key}", permissions=permissions, issues_visibility="all")
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    lookups = (TrackerFactory.build(), StatusFactory.build(name="Dev New"), PriorityFactory.build())
    db_session.add_all(lookups)
    editor = UserFactory.build(login="devact_editor", status="active")
    viewer = UserFactory.build(login="devact_viewer", status="active")
    outsider = UserFactory.build(login="devact_outsider", status="active")
    project = ProjectFactory.build(key="DEV", identifier="dev", name="Development", is_public=False)
    other = ProjectFactory.build(key="DEVX", identifier="devx", name="Other", is_public=False)
    db_session.add_all([editor, viewer, outsider, project, other])
    await db_session.flush()
    for p in (project, other):
        db_session.add(EnabledModule(project_id=p.id, name="issue_tracking"))
    await _member(db_session, editor, project, ["view_issues", "edit_issues"])
    await _member(db_session, editor, other, ["view_issues", "edit_issues"])
    await _member(db_session, viewer, project, ["view_issues"])
    issues = {}
    for n in (1, 2, 3, 4):
        issues[f"DEV-{n}"] = await _issue(db_session, project, n, editor, lookups)
    issues["DEVX-1"] = await _issue(db_session, other, 1, editor, lookups)
    await db_session.commit()
    return World(project, other, issues, editor, viewer, outsider)


@pytest.fixture
def queued(monkeypatch) -> list[int]:
    calls: list[int] = []

    def _record(ids):
        calls.extend(ids)
        return len(calls)

    monkeypatch.setattr("specivo.hooks.router.queue_processing", _record)
    return calls


async def _send(client: AsyncClient, db: AsyncSession, integration, event: str, payload: dict, queued, delivery=None):
    """Post a signed event; process it when accepted. Returns (response, ProcessResult or None)."""
    if integration.provider == "gitlab":
        event = fa.GITLAB_EVENT_HEADERS.get(event, event)
    body = json.dumps(payload).encode()
    headers = signed_headers(integration.provider, body, event=event, delivery=delivery or str(uuid.uuid4()))
    before = len(queued)
    resp = await client.post(hook_path(integration), content=body, headers=headers)
    assert resp.status_code in (200, 202), resp.text
    result = None
    if len(queued) > before:
        result = await process_delivery(db, queued[-1])
        await db.commit()
    return resp, result


def _commit(sha: str, message: str) -> dict:
    data = commit(sha, message)
    data["id"] = sha
    return data


async def _push(client, db, integration, queued, sha: str, message: str, *, branch: str = "main", delivery=None):
    payload = push_payload(integration.provider, [_commit(sha, message)], branch=branch)
    payload["after"] = sha
    return await _send(client, db, integration, "push", payload, queued, delivery=delivery)


async def _headers(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _development(client: AsyncClient, headers, ref: str, **params) -> dict:
    resp = await client.get(f"/api/v1/issues/{ref}/development/", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _count(db: AsyncSession, model, *where) -> int:
    return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


class TestCi:
    async def test_github_runs_of_a_linked_commit_and_their_combined_status(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        await _push(client, db_session, integration, queued, fa.SHA_A, "DEV-1 login form", branch="feat/login")
        for event, payload in (
            ("workflow_run", fa.workflow_run()),
            ("workflow_job", fa.workflow_job()),
            ("check_run", fa.check_run()),
            ("check_suite", fa.check_suite()),
        ):
            _resp, result = await _send(client, db_session, integration, event, payload, queued)
            assert result.status == "processed", result.detail
        headers = await _headers(client, "devact_editor")
        dev = await _development(client, headers, "DEV-1")
        assert {r["run_type"] for r in dev["ci_runs"]} == {"workflow_run", "workflow_job", "check_run", "check_suite"}
        assert dev["summary"]["counts"]["ci"] == 4
        assert dev["summary"]["ci"] == "failing"
        # The failed job passes on its re-run (a new job id, later): the build is passing again.
        await _send(
            client,
            db_session,
            integration,
            "workflow_job",
            fa.workflow_job(job_id=602, conclusion="success", completed_at="2026-10-05T11:00:00Z"),
            queued,
        )
        dev = await _development(client, headers, "DEV-1")
        assert dev["summary"]["ci"] == "passing"
        # Another issue sees none of it.
        assert (await _development(client, headers, "DEV-2"))["ci_runs"] == []

    async def test_runs_link_by_pull_request_number_and_by_branch(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        pr = pull_request_payload("github", number=7, title="DEV-2 profile page", branch="feat/profile")
        await _send(client, db_session, integration, "pull_request", pr, queued)
        await _push(client, db_session, integration, queued, SHA_C, "work in progress", branch="feat/DEV-3-search")
        await _send(
            client,
            db_session,
            integration,
            "workflow_run",
            fa.workflow_run(run_id=1, sha=SHA_D, branch="elsewhere", pull_requests=(7,)),
            queued,
        )
        await _send(
            client,
            db_session,
            integration,
            "workflow_run",
            fa.workflow_run(run_id=2, sha=SHA_D, branch="feat/DEV-3-search"),
            queued,
        )
        await _send(
            client,
            db_session,
            integration,
            "workflow_run",
            fa.workflow_run(run_id=3, sha=SHA_D, branch="feat/profile"),
            queued,
        )
        # A run on the default branch with an unrelated commit belongs to no issue.
        await _send(
            client, db_session, integration, "workflow_run", fa.workflow_run(run_id=4, sha=SHA_D, branch="main"), queued
        )
        headers = await _headers(client, "devact_editor")
        dev2 = await _development(client, headers, "DEV-2")
        dev3 = await _development(client, headers, "DEV-3")
        assert sorted(r["id"] for r in dev2["ci_runs"]) == sorted(
            (await db_session.execute(select(CiRun.id).where(CiRun.external_id.in_([1, 3])))).scalars()
        )
        assert [r["branch"] for r in dev3["ci_runs"]] == ["feat/DEV-3-search"]
        assert await _count(db_session, CiRun) == 4

    async def test_gitlab_pipeline_job_and_deployment(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project, provider="gitlab")
        await _push(client, db_session, integration, queued, fa.SHA_A, "Fixes DEV-1", branch="main")
        for event, payload in (
            ("pipeline", fa.gitlab_pipeline(ref="main")),
            ("job", fa.gitlab_job(ref="main")),
            ("deployment", fa.gitlab_deployment(environment="staging")),
        ):
            _resp, result = await _send(client, db_session, integration, event, payload, queued)
            assert result is not None and result.status == "processed", payload
        dev = await _development(client, await _headers(client, "devact_editor"), "DEV-1")
        assert {(r["run_type"], r["status"]) for r in dev["ci_runs"]} == {("pipeline", "success"), ("job", "failure")}
        assert dev["summary"]["ci"] == "failing"
        (env,) = dev["summary"]["deployments"]
        assert (env["environment_type"], env["environment"]) == ("staging", "staging")
        assert dev["deployments"][0]["status"] == "success"

    async def test_gitea_actions_run(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project, provider="gitea")
        await _push(client, db_session, integration, queued, fa.SHA_A, "DEV-1", branch="feat/login")
        await _send(client, db_session, integration, "workflow_run", fa.workflow_run(conclusion="failure"), queued)
        dev = await _development(client, await _headers(client, "devact_editor"), "DEV-1")
        assert dev["summary"]["ci"] == "failing"
        assert dev["ci_runs"][0]["provider"] == "gitea"


class TestOutOfOrder:
    async def test_an_older_ci_event_never_overwrites_a_newer_one(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        await _send(
            client, db_session, integration, "workflow_run", fa.workflow_run(updated_at="2026-10-05T10:00:00Z"), queued
        )
        _resp, result = await _send(
            client,
            db_session,
            integration,
            "workflow_run",
            fa.workflow_run(status="in_progress", conclusion=None, updated_at="2026-10-05T09:58:00Z"),
            queued,
        )
        assert "older" in result.detail
        run = (await db_session.execute(select(CiRun))).scalar_one()
        assert run.status == "success"
        # A re-run (attempt 2) is newer, even with a clock behind.
        await _send(
            client,
            db_session,
            integration,
            "workflow_run",
            fa.workflow_run(status="queued", conclusion=None, attempt=2, updated_at="2026-10-05T09:59:00Z"),
            queued,
        )
        await db_session.refresh(run)
        assert (run.status, run.attempt) == ("pending", 2)

    async def test_an_older_deployment_status_keeps_the_newer_state(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        await _send(
            client, db_session, integration, "deployment_status", fa.github_deployment_status(state="success"), queued
        )
        await _send(client, db_session, integration, "deployment", fa.github_deployment(), queued)
        await _send(
            client,
            db_session,
            integration,
            "deployment_status",
            fa.github_deployment_status(state="in_progress", at="2026-10-06T08:01:00Z"),
            queued,
        )
        row = (await db_session.execute(select(Deployment))).scalar_one()
        assert row.status == "success"
        assert row.deployed_at is not None
        # Replaced later by a newer deployment: inactive, still known as deployed.
        await _send(
            client,
            db_session,
            integration,
            "deployment_status",
            fa.github_deployment_status(state="inactive", at="2026-10-08T08:00:00Z"),
            queued,
        )
        await db_session.refresh(row)
        assert row.status == "inactive" and row.deployed_at is not None

    async def test_an_older_review_never_overwrites_a_newer_one(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        await _send(
            client, db_session, integration, "pull_request", pull_request_payload("github", title="DEV-1"), queued
        )
        await _send(
            client,
            db_session,
            integration,
            "pull_request_review",
            fa.github_review(state="changes_requested", submitted_at="2026-10-04T11:00:00Z"),
            queued,
        )
        await _send(
            client,
            db_session,
            integration,
            "pull_request_review",
            fa.github_review(state="approved", submitted_at="2026-10-04T10:00:00Z"),
            queued,
        )
        await _send(
            client, db_session, integration, "pull_request_review", fa.github_review(reviewer="kim-example"), queued
        )
        dev = await _development(client, await _headers(client, "devact_editor"), "DEV-1")
        assert {(r["reviewer_login"], r["state"]) for r in dev["reviews"]} == {
            ("sam-example", "changes_requested"),
            ("kim-example", "approved"),
        }
        assert dev["summary"]["reviews"] == "changes_requested"


class TestReviews:
    async def test_gitlab_approval_and_withdrawal(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project, provider="gitlab")
        opened = pull_request_payload("gitlab", title="DEV-1 search", updated_at="2026-10-02T10:00:00Z")
        await _send(client, db_session, integration, "pull_request", opened, queued)
        approved = pull_request_payload("gitlab", title="DEV-1 search", updated_at="2026-10-02T11:00:00Z")
        approved["object_attributes"]["action"] = "approved"
        approved["user"]["username"] = "sam-example"
        _resp, result = await _send(client, db_session, integration, "pull_request", approved, queued)
        assert "1 review(s) recorded" in result.detail
        assert (await db_session.execute(select(PullRequestReview.state))).scalar_one() == "approved"
        withdrawn = pull_request_payload("gitlab", title="DEV-1 search", updated_at="2026-10-02T12:00:00Z")
        withdrawn["object_attributes"]["action"] = "unapproved"
        withdrawn["user"]["username"] = "sam-example"
        await _send(client, db_session, integration, "pull_request", withdrawn, queued)
        assert await _count(db_session, PullRequestReview) == 0

    async def test_gitea_review_and_unknown_pull_request(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project, provider="gitea")
        _resp, result = await _send(client, db_session, integration, "pull_request_approved", fa.gitea_review(), queued)
        assert "not stored" in result.detail
        await _send(
            client, db_session, integration, "pull_request", pull_request_payload("gitea", title="DEV-1"), queued
        )
        await _send(client, db_session, integration, "pull_request_approved", fa.gitea_review(), queued)
        assert (await db_session.execute(select(PullRequestReview.state))).scalar_one() == "approved"


class TestCaps:
    async def test_ci_runs_per_repository_drop_the_oldest(self, client, db_session, world, queued, monkeypatch):
        monkeypatch.setattr("specivo.core.constants.DEVELOPMENT_CI_RUNS_PER_REPOSITORY", 2)
        integration = await make_integration(db_session, world.project)
        for run_id, hour in ((1, "10"), (2, "11"), (3, "12")):
            await _send(
                client,
                db_session,
                integration,
                "workflow_run",
                fa.workflow_run(run_id=run_id, updated_at=f"2026-10-05T{hour}:00:00Z"),
                queued,
            )
        assert sorted((await db_session.execute(select(CiRun.external_id))).scalars()) == [2, 3]

    async def test_deployments_and_tags_per_repository(self, client, db_session, world, queued, monkeypatch):
        monkeypatch.setattr("specivo.core.constants.DEVELOPMENT_DEPLOYMENTS_PER_REPOSITORY", 1)
        monkeypatch.setattr("specivo.core.constants.DEVELOPMENT_RELEASES_PER_REPOSITORY", 1)
        integration = await make_integration(db_session, world.project)
        await _send(
            client,
            db_session,
            integration,
            "deployment_status",
            fa.github_deployment_status(deployment_id=1, at="2026-10-06T08:00:00Z"),
            queued,
        )
        await _send(
            client,
            db_session,
            integration,
            "deployment_status",
            fa.github_deployment_status(deployment_id=2, at="2026-10-06T09:00:00Z"),
            queued,
        )
        assert list((await db_session.execute(select(Deployment.external_id))).scalars()) == [2]
        await _send(client, db_session, integration, "release", fa.github_release("v1.0"), queued)
        release = fa.github_release("v1.1")
        release["release"]["published_at"] = "2026-10-08T09:00:00Z"
        await _send(client, db_session, integration, "release", release, queued)
        assert list((await db_session.execute(select(RepositoryRelease.tag_name))).scalars()) == ["v1.1"]

    async def test_releases_per_issue_keep_the_newest(self, client, db_session, world, queued, monkeypatch):
        monkeypatch.setattr("specivo.core.constants.DEVELOPMENT_RELEASES_PER_ISSUE", 1)
        integration = await make_integration(db_session, world.project)
        await _push(client, db_session, integration, queued, fa.SHA_A, "DEV-1", branch="feat/x")
        for tag in ("v1.0", "v1.0.1"):
            payload = push_payload("github", [], ref=f"refs/tags/{tag}")
            payload["after"] = fa.SHA_A
            await _send(client, db_session, integration, "push", payload, queued)
        rows = (
            await db_session.execute(
                select(RepositoryRelease.tag_name).join(
                    IssueReleaseLink, IssueReleaseLink.release_id == RepositoryRelease.id
                )
            )
        ).scalars()
        assert list(rows) == ["v1.0.1"]

    async def test_reviewers_per_pull_request(self, client, db_session, world, queued, monkeypatch):
        monkeypatch.setattr("specivo.core.constants.DEVELOPMENT_REVIEWERS_PER_PULL_REQUEST", 1)
        integration = await make_integration(db_session, world.project)
        await _send(
            client, db_session, integration, "pull_request", pull_request_payload("github", title="DEV-1"), queued
        )
        await _send(
            client, db_session, integration, "pull_request_review", fa.github_review(reviewer="a-example"), queued
        )
        await _send(
            client, db_session, integration, "pull_request_review", fa.github_review(reviewer="b-example"), queued
        )
        assert list((await db_session.execute(select(PullRequestReview.reviewer_login))).scalars()) == ["a-example"]

    async def test_ci_runs_read_per_issue(self, client, db_session, world, queued, monkeypatch):
        monkeypatch.setattr("specivo.core.constants.DEVELOPMENT_ACTIVITY_PER_ISSUE", 2)
        integration = await make_integration(db_session, world.project)
        await _push(client, db_session, integration, queued, fa.SHA_A, "DEV-1", branch="feat/login")
        for run_id in (1, 2, 3):
            await _send(client, db_session, integration, "workflow_run", fa.workflow_run(run_id=run_id), queued)
        dev = await _development(client, await _headers(client, "devact_editor"), "DEV-1")
        assert len(dev["ci_runs"]) == 2
        assert dev["summary"]["counts"]["ci"] == 3


class TestConfidentialAndUnused:
    @pytest.mark.parametrize(
        ("event", "payload"),
        [
            ("confidential_issue", fa.gitlab_confidential_issue()),
            ("confidential_note", fa.gitlab_confidential_note()),
            ("issue", fa.gitlab_confidential_issue()),
        ],
    )
    async def test_gitlab_confidential_items_are_never_stored(self, client, db_session, world, queued, event, payload):
        integration = await make_integration(db_session, world.project, provider="gitlab")
        resp, result = await _send(client, db_session, integration, event, payload, queued)
        assert (resp.status_code, resp.json()) == (200, {"status": "ignored"})
        assert result is None and queued == []
        delivery = (await db_session.execute(select(RepositoryDelivery))).scalar_one()
        assert delivery.payload is None
        assert "Secret incident" not in (delivery.detail or "")
        assert "Leaked" not in (delivery.detail or "")

    async def test_confidential_flag_wins_over_a_used_event(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project, provider="gitlab")
        payload = fa.gitlab_pipeline()
        payload["object_attributes"]["confidential"] = True
        resp, _result = await _send(client, db_session, integration, "pipeline", payload, queued)
        assert resp.json() == {"status": "ignored"}
        assert (await db_session.execute(select(RepositoryDelivery.payload))).scalar_one() is None
        assert await _count(db_session, CiRun) == 0

    async def test_unused_signed_event_is_accepted_and_logged_without_payload(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        resp, _result = await _send(
            client, db_session, integration, "star", {"action": "created", "repository": {"full_name": "x/y"}}, queued
        )
        assert (resp.status_code, resp.json()) == (200, {"status": "ignored"})
        delivery = (await db_session.execute(select(RepositoryDelivery))).scalar_one()
        assert (delivery.event, delivery.status, delivery.payload) == ("star", "ignored", None)
        assert "not used" in delivery.detail

    async def test_unsigned_unused_event_is_still_refused(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        resp = await client.post(
            hook_path(integration),
            content=b"{}",
            headers={"X-GitHub-Event": "star", "X-Hub-Signature-256": "sha256=" + "0" * 64},
        )
        assert resp.status_code == 401
        assert await _count(db_session, RepositoryDelivery) == 0


class TestReleases:
    async def test_tag_at_a_linked_commit_and_matching_version(self, client, db_session, world, queued):
        db_session.add(VersionFactory.build(project_id=world.project.id, name="1.0"))
        await db_session.commit()
        integration = await make_integration(db_session, world.project)
        await _push(client, db_session, integration, queued, fa.SHA_A, "Fixes DEV-1", branch="feat/x")
        payload = push_payload("github", [], ref="refs/tags/v1.0")
        payload["after"] = fa.SHA_A
        _resp, result = await _send(client, db_session, integration, "push", payload, queued)
        assert "1 issue(s) newly released" in result.detail
        # The release event comes later and makes the tag a release.
        await _send(client, db_session, integration, "release", fa.github_release("v1.0"), queued)
        dev = await _development(client, await _headers(client, "devact_editor"), "DEV-1")
        (release,) = dev["releases"]
        assert (release["tag_name"], release["is_release"], release["source"], release["version_name"]) == (
            "v1.0",
            True,
            "tag_commit",
            "1.0",
        )
        assert dev["summary"]["released_in"][0]["tag_name"] == "v1.0"

    async def test_default_branch_rule(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        # DEV-1 released in v1 already; DEV-2's commit reached main; DEV-3's request merged into main.
        await _push(client, db_session, integration, queued, fa.SHA_A, "DEV-1", branch="main")
        await _send(client, db_session, integration, "create", fa.github_create_tag("v1"), queued)
        tag = push_payload("github", [], ref="refs/tags/v1")
        tag["after"] = fa.SHA_A
        await _send(client, db_session, integration, "push", tag, queued)
        await _push(client, db_session, integration, queued, fa.SHA_B, "DEV-2 tidy", branch="main")
        await _push(client, db_session, integration, queued, SHA_D, "DEV-4 unrelated", branch="feat/other")
        merged = pull_request_payload(
            "github", number=9, title="DEV-3 search", state="merged", updated_at="2030-01-01T00:00:00Z"
        )
        merged["pull_request"]["merge_commit_sha"] = SHA_C
        await _send(client, db_session, integration, "pull_request", merged, queued)
        tag2 = push_payload("github", [], ref="refs/tags/v2")
        tag2["after"] = SHA_C
        await _send(client, db_session, integration, "push", tag2, queued)
        links = {
            (key, tag, source)
            for key, tag, source in (
                await db_session.execute(
                    select(
                        func.concat(Issue.project_key, "-", Issue.sequence_number),
                        RepositoryRelease.tag_name,
                        IssueReleaseLink.source,
                    )
                    .join(IssueReleaseLink, IssueReleaseLink.issue_id == Issue.id)
                    .join(RepositoryRelease, RepositoryRelease.id == IssueReleaseLink.release_id)
                )
            ).all()
        }
        assert links == {
            ("DEV-1", "v1", "tag_commit"),
            ("DEV-3", "v2", "tag_commit"),
            ("DEV-2", "v2", "default_branch"),
        }

    async def test_a_tag_on_an_unknown_commit_releases_nothing_and_deleting_it_removes_it(
        self, client, db_session, world, queued
    ):
        integration = await make_integration(db_session, world.project, provider="gitlab")
        await _push(client, db_session, integration, queued, fa.SHA_A, "DEV-1", branch="main")
        await _send(client, db_session, integration, "tag_push", fa.gitlab_tag_push("v9", sha=SHA_D), queued)
        assert await _count(db_session, IssueReleaseLink) == 0
        await _send(client, db_session, integration, "release", fa.gitlab_release("v3", sha=fa.SHA_A), queued)
        assert await _count(db_session, IssueReleaseLink) == 1
        await _send(client, db_session, integration, "tag_push", fa.gitlab_tag_push("v3", deleted=True), queued)
        assert await _count(db_session, RepositoryRelease, RepositoryRelease.tag_name == "v3") == 0
        assert await _count(db_session, IssueReleaseLink) == 0

    async def test_gitea_create_tag_with_its_commit(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project, provider="gitea")
        await _push(client, db_session, integration, queued, fa.SHA_A, "DEV-1", branch="feat/x")
        await _send(client, db_session, integration, "create", fa.github_create_tag("v5", sha=fa.SHA_A), queued)
        assert await _count(db_session, IssueReleaseLink) == 1


class TestIgnore:
    async def test_ignore_in_a_commit_message_links_nothing_even_from_the_branch(
        self, client, db_session, world, queued
    ):
        integration = await make_integration(db_session, world.project)
        await _push(client, db_session, integration, queued, fa.SHA_A, "Revert, ignore DEV-1", branch="feat/DEV-1-x")
        assert await _count(db_session, IssueCommitLink) == 0

    async def test_ignore_in_a_pull_request_body(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        pr = pull_request_payload("github", title="DEV-1 and DEV-2", body="Ignore DEV-2", branch="feat/DEV-2")
        await _send(client, db_session, integration, "pull_request", pr, queued)
        linked = set((await db_session.execute(select(IssuePullRequestLink.issue_id))).scalars())
        assert linked == {world.issues["DEV-1"].id}


class TestDetachAndMove:
    async def _linked_commit(self, client, db_session, world, queued, *, delivery="push-1"):
        integration = await make_integration(db_session, world.project)
        await _push(client, db_session, integration, queued, fa.SHA_A, "DEV-1 work", branch="feat/x", delivery=delivery)
        dev = await _development(client, await _headers(client, "devact_editor"), "DEV-1")
        return integration, dev["commits"][0]["id"]

    async def test_detach_needs_edit_and_is_audited(self, client, db_session, world, queued):
        _integration, commit_id = await self._linked_commit(client, db_session, world, queued)
        url = f"/api/v1/issues/DEV-1/development/commits/{commit_id}/"
        assert (await client.delete(url, headers=await _headers(client, "devact_viewer"))).status_code == 403
        assert (await client.delete(url, headers=await _headers(client, "devact_outsider"))).status_code == 404
        resp = await client.delete(url, headers=await _headers(client, "devact_editor"))
        assert resp.status_code == 204, resp.text
        assert await _count(db_session, IssueCommitLink) == 0
        audit = (
            await db_session.execute(
                select(SecurityAuditLog).where(SecurityAuditLog.event_type == "development_link_detached")
            )
        ).scalar_one()
        assert audit.user_id == world.editor.id
        assert audit.details["item"] == fa.SHA_A and audit.details["issue"] == "DEV-1"
        issue = await db_session.get(Issue, world.issues["DEV-1"].id)
        await db_session.refresh(issue)
        assert fa.SHA_A not in (issue.issue_metadata or {}).get("commits", [])
        # Detaching again: nothing is linked any more.
        assert (await client.delete(url, headers=await _headers(client, "devact_editor"))).status_code == 404

    async def test_redelivery_does_not_relink_a_detached_commit(self, client, db_session, world, queued):
        integration, commit_id = await self._linked_commit(client, db_session, world, queued)
        headers = await _headers(client, "devact_editor")
        await client.delete(f"/api/v1/issues/DEV-1/development/commits/{commit_id}/", headers=headers)
        # The forge redelivers (a new delivery id), and the same push comes again.
        await _push(client, db_session, integration, queued, fa.SHA_A, "DEV-1 work", branch="feat/x", delivery="push-2")
        assert await _count(db_session, IssueCommitLink) == 0
        assert await _count(db_session, DevelopmentDetachment) == 1

    async def test_move_commit_takes_its_ci_along(self, client, db_session, world, queued):
        integration, commit_id = await self._linked_commit(client, db_session, world, queued)
        await _send(client, db_session, integration, "workflow_run", fa.workflow_run(branch="feat/x"), queued)
        headers = await _headers(client, "devact_editor")
        resp = await client.post(
            f"/api/v1/issues/DEV-1/development/commits/{commit_id}/move/",
            json={"target_issue_ref": "DEV-2"},
            headers=headers,
        )
        assert resp.status_code == 204, resp.text
        dev1 = await _development(client, headers, "DEV-1")
        dev2 = await _development(client, headers, "DEV-2")
        assert dev1["commits"] == [] and dev1["ci_runs"] == []
        assert [c["sha"] for c in dev2["commits"]] == [fa.SHA_A]
        assert len(dev2["ci_runs"]) == 1
        link = (await db_session.execute(select(IssueCommitLink))).scalar_one()
        assert (link.issue_id, link.source) == (world.issues["DEV-2"].id, "manual")
        audit = (
            await db_session.execute(
                select(SecurityAuditLog).where(SecurityAuditLog.event_type == "development_link_moved")
            )
        ).scalar_one()
        assert audit.details["target_issue"] == "DEV-2"
        issue2 = await db_session.get(Issue, world.issues["DEV-2"].id)
        await db_session.refresh(issue2)
        assert issue2.issue_metadata["commits"] == [fa.SHA_A]

    async def test_move_outside_the_scope_or_without_rights_is_refused(self, client, db_session, world, queued):
        _integration, commit_id = await self._linked_commit(client, db_session, world, queued)
        url = f"/api/v1/issues/DEV-1/development/commits/{commit_id}/move/"
        editor = await _headers(client, "devact_editor")
        resp = await client.post(url, json={"target_issue_ref": "DEVX-1"}, headers=editor)
        assert resp.status_code == 422, resp.text
        viewer = await _headers(client, "devact_viewer")
        assert (await client.post(url, json={"target_issue_ref": "DEV-2"}, headers=viewer)).status_code == 403
        assert await _count(db_session, IssueCommitLink, IssueCommitLink.issue_id == world.issues["DEV-1"].id) == 1

    async def test_detach_and_move_a_pull_request(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        pr = pull_request_payload("github", title="DEV-1 and DEV-2", updated_at="2026-10-02T10:00:00Z")
        await _send(client, db_session, integration, "pull_request", pr, queued)
        headers = await _headers(client, "devact_editor")
        pr_id = (await _development(client, headers, "DEV-1"))["pull_requests"][0]["id"]
        resp = await client.post(
            f"/api/v1/issues/DEV-1/development/pull-requests/{pr_id}/move/",
            json={"target_issue_ref": "DEV-3"},
            headers=headers,
        )
        assert resp.status_code == 204, resp.text
        resp = await client.delete(f"/api/v1/issues/DEV-2/development/pull-requests/{pr_id}/", headers=headers)
        assert resp.status_code == 204, resp.text
        # An edit of the request still names DEV-1 and DEV-2: neither is linked again.
        pr = pull_request_payload("github", title="DEV-1 and DEV-2", updated_at="2026-10-03T10:00:00Z")
        await _send(client, db_session, integration, "pull_request", pr, queued)
        linked = set((await db_session.execute(select(IssuePullRequestLink.issue_id))).scalars())
        assert linked == {world.issues["DEV-3"].id}


class TestReadAccess:
    async def test_timeline_paging_and_filters(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        await _push(client, db_session, integration, queued, fa.SHA_A, "DEV-1 one", branch="feat/x")
        await _push(client, db_session, integration, queued, fa.SHA_B, "DEV-1 two", branch="feat/x")
        await _send(client, db_session, integration, "workflow_run", fa.workflow_run(branch="feat/x"), queued)
        await _send(
            client, db_session, integration, "pull_request", pull_request_payload("github", title="DEV-1"), queued
        )
        headers = await _headers(client, "devact_editor")
        dev = await _development(client, headers, "DEV-1")
        timeline = dev["timeline"]
        assert timeline["total_count"] == 4
        assert {item["kind"] for item in timeline["items"]} == {"commit", "ci", "pull_request"}
        times = [item["occurred_at"] for item in timeline["items"]]
        assert times == sorted(times, reverse=True)
        page = (await _development(client, headers, "DEV-1", limit=1, offset=1))["timeline"]
        assert (page["total_count"], page["offset"], page["limit"]) == (4, 1, 1)
        assert page["items"] == timeline["items"][1:2]
        only = (await _development(client, headers, "DEV-1", kinds=["commit", "ci"]))["timeline"]
        assert only["total_count"] == 3 and {i["kind"] for i in only["items"]} == {"commit", "ci"}
        # The existing fields are still there.
        assert dev["commit_count"] == 2 and len(dev["pull_requests"]) == 1
        bad = await client.get("/api/v1/issues/DEV-1/development/", params={"kinds": "bogus"}, headers=headers)
        assert bad.status_code == 422

    async def test_mcp_tool_returns_the_rest_document(self, client, db_session, world, queued):
        from specivo.mcp.tools import _issue_development, _show_issue

        integration = await make_integration(db_session, world.project)
        await _push(client, db_session, integration, queued, fa.SHA_A, "DEV-1 one", branch="feat/x")
        await _send(
            client,
            db_session,
            integration,
            "workflow_run",
            fa.workflow_run(branch="feat/x", conclusion="failure"),
            queued,
        )
        headers = await _headers(client, "devact_editor")
        rest = await _development(client, headers, "DEV-1", kinds=["ci"], limit=5)
        editor = await db_session.get(User, world.editor.id)
        mcp = json.loads(await _issue_development(db_session, editor, "DEV-1", kinds=["ci"], limit=5))
        assert mcp == rest
        shown = await _show_issue(db_session, editor, "DEV-1", metadata_only=True)
        assert "Development: CI failing (1 runs)" in shown

    async def test_nobody_else_sees_activity(self, client, db_session, world, queued):
        from specivo.mcp.tools import _issue_development

        integration = await make_integration(db_session, world.project)
        await _push(client, db_session, integration, queued, fa.SHA_A, "DEV-1 zanzibarquux", branch="feat/x")
        await _send(client, db_session, integration, "workflow_run", fa.workflow_run(branch="feat/x"), queued)
        outsider_headers = await _headers(client, "devact_outsider")
        resp = await client.get("/api/v1/issues/DEV-1/development/", headers=outsider_headers)
        assert resp.status_code == 404
        outsider = await db_session.get(User, world.outsider.id)
        with pytest.raises((NotFoundError, PermissionDeniedError)):
            await _issue_development(db_session, outsider, "DEV-1")
        search = await client.get(
            "/api/v1/search/", params={"q": "zanzibarquux", "mode": "keyword"}, headers=outsider_headers
        )
        assert "zanzibarquux" not in search.text
        assert "DEV-1" not in search.text


class TestEnvironmentOverride:
    async def test_patch_and_use(self, client, db_session, world, queued):
        role = (await db_session.execute(select(Role).where(Role.name == "devact-devact_editor-DEV"))).scalar_one()
        role.permissions = [*role.permissions, "manage_project"]
        await db_session.commit()
        integration = await make_integration(db_session, world.project)
        headers = await _headers(client, "devact_editor")
        url = f"/api/v1/projects/DEV/repositories/{integration.id}/"
        resp = await client.patch(
            url, json={"settings": {"environment_types": {"Pages": "production", "qa*": "staging"}}}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["settings"]["environment_types"] == {"Pages": "production", "qa*": "staging"}
        for body in (
            {"environment_types": {"pages": "moon"}},
            {"environment_types": {"": "production"}},
            {"environment_types": {"a": "production", "A": "testing"}},
            {"environment_types": {f"e{i}": "testing" for i in range(101)}},
        ):
            assert (await client.patch(url, json={"settings": body}, headers=headers)).status_code == 422, body
        await _push(client, db_session, integration, queued, fa.SHA_A, "DEV-1", branch="main")
        await db_session.refresh(integration)
        for deployment_id, environment, at in (
            (1, "pages", "2026-10-06T08:00:00Z"),
            (2, "qa-eu", "2026-10-06T09:00:00Z"),
            (3, "eu-west", "2026-10-06T10:00:00Z"),
        ):
            await _send(
                client,
                db_session,
                integration,
                "deployment_status",
                fa.github_deployment_status(deployment_id=deployment_id, environment=environment, at=at),
                queued,
            )
        dev = await _development(client, headers, "DEV-1")
        assert {(d["environment"], d["environment_type"]) for d in dev["deployments"]} == {
            ("pages", "production"),
            ("qa-eu", "staging"),
            ("eu-west", None),
        }
        assert [e["environment_type"] for e in dev["summary"]["deployments"]] == ["production", "staging", None]
