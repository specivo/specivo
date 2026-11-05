"""Changing the provider of a connected repository, and the address checks on create and update.

A provider change moves the hook URL (it names the provider): the old URL
answers 404 at once, the new one verifies with the new forge's scheme, and a
new secret is returned once. Commits and pull requests already linked stay;
deliveries and pull requests keep the provider they came from. The change is
audited. Celery is not running: the endpoint's queueing is recorded and the
tests run ``process_delivery`` themselves.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.repository import (
    IssueCommitLink,
    IssuePullRequestLink,
    PullRequest,
    RepositoryIntegration,
)
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.services.repository_hooks import process_delivery
from specivo.services.security_audit_service import AuditEvent
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory
from tests.fixtures.forge_hooks import commit, pull_request_payload, push_payload, signed_headers

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_REPO = "example-org/widgets"
_GITHUB = {"provider": "github", "repo_full_name": _REPO, "repo_url": f"https://github.com/{_REPO}"}
_TO_GITLAB = {"provider": "gitlab", "repo_url": f"https://gitlab.com/{_REPO}"}


@dataclass
class World:
    project: Project
    issue_id: int
    manager: dict[str, str]
    viewer: dict[str, str]


async def _token(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _member(db: AsyncSession, project: Project, login: str, permissions: list[str]) -> None:
    user = UserFactory.build(login=login, status="active")
    role = Role(name=f"rpch-{login}", permissions=permissions)
    db.add_all([user, role])
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()


@pytest_asyncio.fixture
async def world(client: AsyncClient, db_session: AsyncSession) -> World:
    tracker, priority, status = TrackerFactory.build(), PriorityFactory.build(), StatusFactory.build()
    author = UserFactory.build(login="rpch_author", status="active")
    project = ProjectFactory.build(key="RPCH", identifier="rpch", name="Provider change", is_public=False)
    db_session.add_all([tracker, priority, status, author, project])
    await db_session.flush()
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=author.id,
        subject="Provider change login form",
    )
    db_session.add(issue)
    await db_session.commit()
    await _member(db_session, project, "rpch_manager", ["view_issues", "manage_project"])
    await _member(db_session, project, "rpch_viewer", ["view_issues"])
    return World(project, issue.id, await _token(client, "rpch_manager"), await _token(client, "rpch_viewer"))


@pytest.fixture
def queued(monkeypatch) -> list[int]:
    calls: list[int] = []

    def _record(ids):
        calls.extend(ids)
        return len(calls)

    monkeypatch.setattr("specivo.hooks.router.queue_processing", _record)
    return calls


def _base(world: World) -> str:
    return f"/api/v1/projects/{world.project.key}/repositories/"


async def _connect(client: AsyncClient, world: World, **body) -> dict:
    resp = await client.post(_base(world), json={**_GITHUB, **body}, headers=world.manager)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _post(client: AsyncClient, hook_url: str, provider: str, payload: dict, secret: str, event: str):
    body = json.dumps(payload).encode()
    headers = signed_headers(provider, body, secret=secret, event=event, delivery=str(uuid.uuid4()))
    return await client.post(urlparse(hook_url).path, content=body, headers=headers)


async def _deliver(client, db, queued, hook_url, provider, payload, secret, event):
    resp = await _post(client, hook_url, provider, payload, secret, event)
    assert resp.status_code == 202, resp.text
    result = await process_delivery(db, queued[-1])
    await db.commit()
    return result


async def _links(db: AsyncSession, issue_id: int) -> tuple[int, int]:
    commits = (await db.execute(select(IssueCommitLink).where(IssueCommitLink.issue_id == issue_id))).all()
    prs = (await db.execute(select(IssuePullRequestLink).where(IssuePullRequestLink.issue_id == issue_id))).all()
    return len(commits), len(prs)


class TestProviderChange:
    async def test_new_url_and_secret_old_url_gone_history_kept(self, client, db_session, world, queued):
        created = await _connect(client, world)
        old_url, old_secret = created["hook_url"], created["secret"]
        assert "/hooks/github/" in old_url

        # History from GitHub: a commit and a pull request on RPCH-1.
        push = push_payload("github", [commit("one", "Fixes RPCH-1 login form")], repo=_REPO)
        await _deliver(client, db_session, queued, old_url, "github", push, old_secret, "push")
        pr = pull_request_payload("github", number=7, title="GitHub request", body="Fixes RPCH-1", repo=_REPO)
        await _deliver(client, db_session, queued, old_url, "github", pr, old_secret, "pull_request")
        assert await _links(db_session, world.issue_id) == (1, 1)

        resp = await client.patch(f"{_base(world)}{created['id']}/", json=_TO_GITLAB, headers=world.manager)
        assert resp.status_code == 200, resp.text
        changed = resp.json()
        assert changed["provider"] == "gitlab"
        assert changed["public_id"] == created["public_id"]
        assert changed["hook_url"] == old_url.replace("/hooks/github/", "/hooks/gitlab/")
        new_secret = changed["secret"]
        assert new_secret and new_secret != old_secret

        # The old URL is gone at once, whatever it is sent.
        old = await _post(client, old_url, "github", push, old_secret, "push")
        assert old.status_code == 404
        # The new URL verifies GitLab's token: not GitHub's signature, not the old secret.
        assert (await _post(client, changed["hook_url"], "github", push, new_secret, "push")).status_code == 401
        gl_push = push_payload("gitlab", [commit("two", "Refs RPCH-1 more")], repo=_REPO)
        assert (await _post(client, changed["hook_url"], "gitlab", gl_push, old_secret, "push")).status_code == 401
        await _deliver(client, db_session, queued, changed["hook_url"], "gitlab", gl_push, new_secret, "push")

        # The same number on GitLab is another request: GitHub's #7 stays as it was.
        mr = pull_request_payload("gitlab", number=7, title="GitLab request", body="Fixes RPCH-1", repo=_REPO)
        await _deliver(client, db_session, queued, changed["hook_url"], "gitlab", mr, new_secret, "pull_request")
        rows = (
            await db_session.execute(
                select(PullRequest.provider, PullRequest.title).where(PullRequest.integration_id == created["id"])
            )
        ).all()
        assert sorted(rows) == [("github", "GitHub request"), ("gitlab", "GitLab request")]
        assert await _links(db_session, world.issue_id) == (2, 2)

        # The development panel labels each request with its own forge.
        dev = await client.get("/api/v1/issues/RPCH-1/development/", headers=world.manager)
        assert dev.status_code == 200, dev.text
        assert sorted(p["provider"] for p in dev.json()["pull_requests"]) == ["github", "gitlab"]

        # Old deliveries keep their provider; new ones carry the new one.
        log = await client.get(f"{_base(world)}{created['id']}/deliveries/?limit=50", headers=world.manager)
        providers = [d["provider"] for d in log.json()["items"]]
        assert providers == ["gitlab", "gitlab", "github", "github"]

    async def test_change_is_audited_without_the_secret(self, client, db_session, world):
        created = await _connect(client, world)
        resp = await client.patch(f"{_base(world)}{created['id']}/", json=_TO_GITLAB, headers=world.manager)
        assert resp.status_code == 200, resp.text
        rows = (
            (
                await db_session.execute(
                    select(SecurityAuditLog).where(
                        SecurityAuditLog.event_type == AuditEvent.REPOSITORY_PROVIDER_CHANGED,
                        SecurityAuditLog.project_id == world.project.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.resource_type == "repository_integration"
        assert row.resource_id == created["id"]
        assert row.details == {
            "old_provider": "github",
            "new_provider": "gitlab",
            "old_repository": _REPO,
            "new_repository": _REPO,
            "secret_rotated": True,
        }
        assert resp.json()["secret"] not in json.dumps(row.details)

    async def test_repository_rename_with_the_provider_is_audited(self, client, db_session, world):
        created = await _connect(client, world)
        body = {"provider": "gitlab", "repo_full_name": "example-group/sub/widgets"}
        body["repo_url"] = "https://gitlab.com/example-group/sub/widgets/-/tree/main"
        resp = await client.patch(f"{_base(world)}{created['id']}/", json=body, headers=world.manager)
        assert resp.status_code == 200, resp.text
        assert resp.json()["repo_url"] == "https://gitlab.com/example-group/sub/widgets"
        details = (
            await db_session.execute(
                select(SecurityAuditLog.details).where(
                    SecurityAuditLog.event_type == AuditEvent.REPOSITORY_PROVIDER_CHANGED,
                    SecurityAuditLog.project_id == world.project.id,
                )
            )
        ).scalar_one()
        assert (details["old_repository"], details["new_repository"]) == (_REPO, "example-group/sub/widgets")

    async def test_same_provider_rotates_nothing_and_audits_nothing(self, client, db_session, world):
        created = await _connect(client, world)
        resp = await client.patch(
            f"{_base(world)}{created['id']}/", json={"provider": "github", "scope": "project"}, headers=world.manager
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["secret"] is None
        assert resp.json()["hook_url"] == created["hook_url"]
        count = (
            await db_session.execute(
                select(SecurityAuditLog.id).where(
                    SecurityAuditLog.event_type == AuditEvent.REPOSITORY_PROVIDER_CHANGED,
                    SecurityAuditLog.project_id == world.project.id,
                )
            )
        ).all()
        assert count == []

    async def test_delivery_queued_before_the_change_is_not_read_as_the_new_forge(
        self, client, db_session, world, queued
    ):
        created = await _connect(client, world)
        push = push_payload("github", [commit("three", "Fixes RPCH-1")], repo=_REPO)
        resp = await _post(client, created["hook_url"], "github", push, created["secret"], "push")
        assert resp.status_code == 202
        pending = queued[-1]
        changed = await client.patch(f"{_base(world)}{created['id']}/", json=_TO_GITLAB, headers=world.manager)
        assert changed.status_code == 200, changed.text
        db_session.expire_all()
        result = await process_delivery(db_session, pending)
        await db_session.commit()
        assert result is not None and result.status == "ignored"
        assert "now uses gitlab" in (result.detail or "")
        assert await _links(db_session, world.issue_id) == (0, 0)

    async def test_user_without_manage_project_is_refused(self, client, db_session, world):
        created = await _connect(client, world)
        resp = await client.patch(f"{_base(world)}{created['id']}/", json=_TO_GITLAB, headers=world.viewer)
        assert resp.status_code == 403
        stored = await db_session.get(RepositoryIntegration, created["id"])
        await db_session.refresh(stored)
        assert stored.provider == "github"

    async def test_moving_onto_a_repository_already_connected_is_a_conflict(self, client, world):
        await _connect(client, world, provider="gitlab", repo_url=f"https://gitlab.com/{_REPO}")
        created = await _connect(client, world)
        resp = await client.patch(f"{_base(world)}{created['id']}/", json=_TO_GITLAB, headers=world.manager)
        assert resp.status_code == 409
        assert resp.json()["errors"][0]["field"] == "repo_full_name"


class TestAddressChecks:
    @pytest.mark.parametrize(
        ("override", "field"),
        [
            ({"provider": "gitlab"}, "provider"),
            ({"provider": "gitea", "repo_url": "https://gitlab.com/example-org/widgets"}, "provider"),
            ({"repo_full_name": "example-org/gadgets"}, "repo_full_name"),
            ({"provider": "gitea", "repo_url": "https://git.example.org/example-org/gadgets"}, "repo_full_name"),
            ({"provider": "gitea", "repo_url": "https://git.example.org/"}, "repo_url"),
        ],
    )
    async def test_create_mismatch_is_422_on_the_field(self, client, world, override, field):
        resp = await client.post(_base(world), json={**_GITHUB, **override}, headers=world.manager)
        assert resp.status_code == 422, resp.text
        assert resp.json()["errors"][0]["field"] == field

    @pytest.mark.parametrize(
        ("body", "field"),
        [
            ({"provider": "gitlab"}, "provider"),
            ({"repo_full_name": "example-org/gadgets"}, "repo_full_name"),
            ({"repo_url": "https://github.com/example-org/gadgets"}, "repo_full_name"),
        ],
    )
    async def test_update_mismatch_is_422_on_the_field(self, client, db_session, world, body, field):
        created = await _connect(client, world)
        resp = await client.patch(f"{_base(world)}{created['id']}/", json=body, headers=world.manager)
        assert resp.status_code == 422, resp.text
        assert resp.json()["errors"][0]["field"] == field
        stored = await db_session.get(RepositoryIntegration, created["id"])
        await db_session.refresh(stored)
        assert (stored.provider, stored.repo_full_name) == ("github", _REPO)

    async def test_ssh_and_sub_page_addresses_are_stored_as_the_web_url(self, client, world):
        ssh = await _connect(client, world, repo_url=f"git@github.com:{_REPO}.git")
        assert ssh["repo_url"] == f"https://github.com/{_REPO}"
        page = await _connect(
            client,
            world,
            provider="forgejo",
            repo_full_name="example-org/gadgets",
            repo_url="https://codeberg.org/example-org/gadgets/src/branch/main",
        )
        assert page["repo_url"] == "https://codeberg.org/example-org/gadgets"

    async def test_self_hosted_under_a_sub_path_is_accepted_as_given(self, client, world):
        url = f"https://example.org/git/{_REPO}"
        created = await _connect(client, world, provider="gitea", repo_url=url)
        assert created["repo_url"] == url

    async def test_toggling_needs_no_address_check(self, client, db_session, world):
        created = await _connect(client, world)
        # A row stored before the checks existed, with a URL that names nothing.
        stored = await db_session.get(RepositoryIntegration, created["id"])
        stored.repo_url = "https://example.org/"
        await db_session.commit()
        resp = await client.patch(f"{_base(world)}{created['id']}/", json={"is_active": False}, headers=world.manager)
        assert resp.status_code == 200, resp.text
