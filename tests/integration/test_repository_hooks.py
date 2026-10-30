"""The incoming repository webhook endpoint, end to end: ``POST /hooks/{provider}/{public_id}/``.

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

from specivo.core.config import get_settings
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.project import Project
from specivo.models.repository import IssueCommitLink, RepositoryCommit, RepositoryDelivery
from specivo.models.user import User
from specivo.services.repository_hooks import process_delivery
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory
from tests.fixtures.forge_hooks import (
    REPO,
    SECRET,
    commit,
    fixture_payload,
    hook_path,
    make_integration,
    push_payload,
    signed_headers,
)

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@dataclass
class World:
    project: Project
    other: Project
    author: User
    issues: dict[str, Issue]


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


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    lookups = (TrackerFactory.build(), StatusFactory.build(), PriorityFactory.build())
    db_session.add_all(lookups)
    author = UserFactory.build(login="rhook_author", status="active")
    project = ProjectFactory.build(key="RHOOK", identifier="rhook", name="Hooks", is_public=False)
    other = ProjectFactory.build(key="RHOOP", identifier="rhoop", name="Private other", is_public=False)
    db_session.add_all([author, project, other])
    await db_session.flush()
    issues = {}
    for n in (1, 2, 3):
        issues[f"RHOOK-{n}"] = await _issue(db_session, project, n, author, lookups)
    issues["RHOOP-1"] = await _issue(db_session, other, 1, author, lookups)
    await db_session.commit()
    return World(project, other, author, issues)


@pytest.fixture
def queued(monkeypatch) -> list[int]:
    """Delivery row ids the endpoint queued for processing."""
    calls: list[int] = []

    def _record(ids):
        calls.extend(ids)
        return len(calls)

    monkeypatch.setattr("specivo.hooks.router.queue_processing", _record)
    return calls


async def _post(client: AsyncClient, integration, payload, *, event="push", delivery=None, secret=SECRET, headers=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    sent = signed_headers(integration.provider, body, secret=secret, event=event, delivery=delivery)
    if headers:
        sent |= headers
    return await client.post(hook_path(integration), content=body, headers=sent)


async def _push_and_process(client, db_session, integration, payload, queued, *, delivery=None):
    resp = await _post(client, integration, payload, delivery=delivery)
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"status": "accepted"}
    result = await process_delivery(db_session, queued[-1])
    await db_session.commit()
    return result


async def _journals(db: AsyncSession, issue: Issue) -> list[Journal]:
    rows = await db.execute(select(Journal).where(Journal.issue_id == issue.id).order_by(Journal.sequence))
    return list(rows.scalars().all())


async def _git_user(db: AsyncSession) -> User:
    return (await db.execute(select(User).where(User.login == "$git"))).scalar_one()


async def _count(db: AsyncSession, model) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


class TestPush:
    async def test_push_links_commit_writes_git_journal_and_metadata(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        c = commit("one", "Add the login form\n\nrefs #RHOOK-1")
        await _push_and_process(client, db_session, integration, push_payload("github", [c]), queued)

        stored = (await db_session.execute(select(RepositoryCommit))).scalar_one()
        assert (stored.sha, stored.branch, stored.author_name) == (c["id"], "main", "Alex Example")
        link = (await db_session.execute(select(IssueCommitLink))).scalar_one()
        assert (link.issue_id, link.source, link.keyword) == (world.issues["RHOOK-1"].id, "message", "refs")

        (journal,) = await _journals(db_session, world.issues["RHOOK-1"])
        git = await _git_user(db_session)
        assert journal.user_id == git.id
        assert journal.user_id != world.author.id
        assert journal.notes == (f"Commit [`{c['id'][:7]}`]({c['url']}) on `main` by Alex Example: Add the login form")

        issue = await db_session.get(Issue, world.issues["RHOOK-1"].id)
        await db_session.refresh(issue)
        assert issue.issue_metadata["commits"] == [c["id"]]

        delivery = (await db_session.execute(select(RepositoryDelivery))).scalar_one()
        assert delivery.status == "processed"
        assert "1 new link(s) to 1 issue(s)" in delivery.detail
        assert "added" not in json.dumps(delivery.payload)
        await db_session.refresh(integration)
        assert integration.last_delivery_at is not None

    @pytest.mark.parametrize(
        "message",
        ["refs #RHOOK-2", "RHOOK-2 tidy up", "Fixes RHOOK-2"],
    )
    async def test_reference_forms(self, client, db_session, world, queued, message):
        integration = await make_integration(db_session, world.project)
        await _push_and_process(
            client, db_session, integration, push_payload("github", [commit(message, message)]), queued
        )
        link = (await db_session.execute(select(IssueCommitLink))).scalar_one()
        assert link.issue_id == world.issues["RHOOK-2"].id
        assert link.keyword == ("fixes" if message.startswith("Fixes") else "refs")

    async def test_branch_name_links(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        payload = push_payload("github", [commit("b", "Style the form")], branch="feat/rhook-3-login-form")
        await _push_and_process(client, db_session, integration, payload, queued)
        link = (await db_session.execute(select(IssueCommitLink))).scalar_one()
        assert (link.issue_id, link.source, link.keyword) == (world.issues["RHOOK-3"].id, "branch", None)

    async def test_one_commit_several_issues(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        payload = push_payload("github", [commit("m", "Fixes RHOOK-1, RHOOK-2")], branch="RHOOK-3-work")
        await _push_and_process(client, db_session, integration, payload, queued)
        assert await _count(db_session, RepositoryCommit) == 1
        assert await _count(db_session, IssueCommitLink) == 3
        for key in ("RHOOK-1", "RHOOK-2", "RHOOK-3"):
            assert len(await _journals(db_session, world.issues[key])) == 1

    async def test_commit_without_references_is_not_stored(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        result = await _push_and_process(
            client, db_session, integration, push_payload("github", [commit("n", "No refs here")]), queued
        )
        assert result.status == "processed"
        assert await _count(db_session, RepositoryCommit) == 0

    async def test_reference_to_missing_issue_is_dropped(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        await _push_and_process(
            client, db_session, integration, push_payload("github", [commit("x", "RHOOK-99")]), queued
        )
        assert await _count(db_session, IssueCommitLink) == 0
        assert await _count(db_session, RepositoryCommit) == 0

    async def test_tag_push_is_ignored(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        payload = push_payload("github", [commit("t", "RHOOK-1")], ref="refs/tags/v1.0")
        result = await _push_and_process(client, db_session, integration, payload, queued)
        assert result.status == "ignored"
        assert await _count(db_session, IssueCommitLink) == 0

    async def test_default_branch_refreshed(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        payload = push_payload("github", [commit("d", "RHOOK-1")])
        payload["repository"]["default_branch"] = "trunk"
        await _push_and_process(client, db_session, integration, payload, queued)
        await db_session.refresh(integration)
        assert integration.default_branch == "trunk"

    async def test_metadata_append_skips_existing_sha(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        c = commit("dup", "RHOOK-1")
        issue = world.issues["RHOOK-1"]
        issue.issue_metadata = {"commits": [c["id"]], "component": "web"}
        await db_session.commit()
        await _push_and_process(client, db_session, integration, push_payload("github", [c]), queued)
        await db_session.refresh(issue)
        assert issue.issue_metadata == {"commits": [c["id"]], "component": "web"}

    async def test_metadata_key_of_another_type_is_left_alone(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        issue = world.issues["RHOOK-1"]
        issue.issue_metadata = {"commits": "see wiki"}
        await db_session.commit()
        await _push_and_process(
            client, db_session, integration, push_payload("github", [commit("o", "RHOOK-1")]), queued
        )
        await db_session.refresh(issue)
        assert issue.issue_metadata == {"commits": "see wiki"}
        assert await _count(db_session, IssueCommitLink) == 1


class TestIdempotency:
    async def test_same_delivery_twice(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        payload = push_payload("github", [commit("i", "RHOOK-1")])
        await _push_and_process(client, db_session, integration, payload, queued, delivery="d-1")
        again = await _post(client, integration, payload, delivery="d-1")
        assert again.status_code == 200
        assert again.json() == {"status": "duplicate"}
        assert len(queued) == 1
        assert await _count(db_session, RepositoryDelivery) == 1
        assert await _count(db_session, IssueCommitLink) == 1
        assert len(await _journals(db_session, world.issues["RHOOK-1"])) == 1

    async def test_same_commits_new_delivery_id(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        payload = push_payload("github", [commit("i", "RHOOK-1")])
        await _push_and_process(client, db_session, integration, payload, queued, delivery="d-1")
        result = await _push_and_process(client, db_session, integration, payload, queued, delivery="d-2")
        assert result.status == "processed"
        assert result.new_links == []
        assert await _count(db_session, RepositoryDelivery) == 2
        assert await _count(db_session, IssueCommitLink) == 1
        assert len(await _journals(db_session, world.issues["RHOOK-1"])) == 1

    async def test_processing_twice_does_nothing_more(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        await _push_and_process(
            client, db_session, integration, push_payload("github", [commit("i", "RHOOK-1")]), queued
        )
        assert await process_delivery(db_session, queued[-1]) is None
        assert len(await _journals(db_session, world.issues["RHOOK-1"])) == 1

    async def test_body_hash_dedupes_without_delivery_header(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project, provider="gitlab")
        payload = push_payload("gitlab", [commit("g", "RHOOK-1")])
        first = await _post(client, integration, payload)
        second = await _post(client, integration, payload)
        assert (first.status_code, second.json()) == (202, {"status": "duplicate"})


class TestScope:
    async def test_private_project_outside_scope_is_untouched(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        in_scope = await _post(client, integration, push_payload("github", [commit("a", "RHOOK-1")]))
        out_of_scope = await _post(client, integration, push_payload("github", [commit("b", "Fixes RHOOP-1")]))
        assert (in_scope.status_code, in_scope.json()) == (out_of_scope.status_code, out_of_scope.json())
        result = await process_delivery(db_session, queued[-1])
        await db_session.commit()
        assert result.status == "processed"
        assert "RHOOP" not in result.detail
        assert await _journals(db_session, world.issues["RHOOP-1"]) == []
        other_issue = world.issues["RHOOP-1"]
        await db_session.refresh(other_issue)
        assert "commits" not in (other_issue.issue_metadata or {})

    async def test_subprojects_only_when_configured(self, client, db_session, world, queued):
        child = ProjectFactory.build(
            key="RHOOC", identifier="rhooc", name="Child", parent_id=world.project.id, path="rhook.rhooc"
        )
        db_session.add(child)
        await db_session.flush()
        lookups = await db_session.get(Issue, world.issues["RHOOK-1"].id)
        child_issue = IssueFactory.build(
            project_id=child.id,
            project_key="RHOOC",
            sequence_number=1,
            tracker_id=lookups.tracker_id,
            status_id=lookups.status_id,
            priority_id=lookups.priority_id,
            author_id=world.author.id,
        )
        db_session.add(child_issue)
        await db_session.commit()

        narrow = await make_integration(db_session, world.project, repo="example-org/narrow")
        await _push_and_process(
            client,
            db_session,
            narrow,
            push_payload("github", [commit("c1", "RHOOC-1")], repo="example-org/narrow"),
            queued,
        )
        assert await _count(db_session, IssueCommitLink) == 0

        admin = AdminUserFactory.build(login="rhook_admin")
        db_session.add(admin)
        await db_session.commit()
        wide = await make_integration(
            db_session,
            world.project,
            repo="example-org/wide",
            scope="project_and_subprojects",
            created_by_id=admin.id,
        )
        await _push_and_process(
            client, db_session, wide, push_payload("github", [commit("c2", "RHOOC-1")], repo="example-org/wide"), queued
        )
        link = (await db_session.execute(select(IssueCommitLink))).scalar_one()
        assert link.issue_id == child_issue.id


class TestRefusals:
    async def test_deeply_nested_json_is_not_a_server_error(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        resp = await _post(client, integration, b"[" * 200_000 + b"]" * 200_000)
        assert resp.status_code < 500
        assert queued == []

    async def test_unknown_inactive_bad_provider_and_bad_id_look_the_same(self, client, db_session, world, queued):
        active = await make_integration(db_session, world.project)
        inactive = await make_integration(db_session, world.project, repo="example-org/off", is_active=False)
        body = json.dumps(push_payload("github", [commit("z", "RHOOK-1")])).encode()
        headers = signed_headers("github", body)
        responses = [
            await client.post(f"/hooks/github/{uuid.uuid4()}/", content=body, headers=headers),
            await client.post(hook_path(inactive), content=body, headers=headers),
            await client.post(f"/hooks/gitlab/{active.public_id}/", content=body, headers=headers),
            await client.post(f"/hooks/bitbucket/{active.public_id}/", content=body, headers=headers),
            await client.post("/hooks/github/not-a-uuid/", content=body, headers=headers),
        ]
        assert [r.status_code for r in responses] == [404] * 5
        assert len({r.text for r in responses}) == 1
        assert await _count(db_session, RepositoryDelivery) == 0

    async def test_old_push_endpoints_are_gone(self, client, world):
        for path in ("/hooks/github/push/", "/hooks/gitlab/push/"):
            assert (await client.post(path, content=b"{}")).status_code == 404

    async def test_bad_signature_is_401_and_records_nothing(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        payload = push_payload("github", [commit("s", "RHOOK-1")])
        resp = await _post(client, integration, payload, secret="not-the-secret")
        assert resp.status_code == 401
        missing = await client.post(hook_path(integration), content=json.dumps(payload).encode())
        assert missing.status_code == 401
        assert await _count(db_session, RepositoryDelivery) == 0
        assert queued == []
        await db_session.refresh(integration)
        assert integration.last_delivery_at is None

    async def test_rotated_secret_replaces_the_old_one(self, client, db_session, world, queued):
        from specivo.services.repository_service import RepositoryIntegrationService

        integration = await make_integration(db_session, world.project)
        _, new_secret = await RepositoryIntegrationService().rotate_secret(
            db_session, integration.id, world.project.id, actor=world.author
        )
        await db_session.commit()
        payload = push_payload("github", [commit("r", "RHOOK-1")])
        assert (await _post(client, integration, payload)).status_code == 401
        assert (await _post(client, integration, payload, secret=new_secret)).status_code == 202

    async def test_body_over_the_limit_is_413(self, client, db_session, world, queued, monkeypatch):
        integration = await make_integration(db_session, world.project)
        monkeypatch.setattr(get_settings(), "git_hook_max_body_bytes", 2048)
        payload = push_payload("github", [commit("big", "RHOOK-1 " + "x" * 4000)])
        resp = await _post(client, integration, payload)
        assert resp.status_code == 413

        async def _stream():
            yield b'{"padding": "'
            for _ in range(10):
                yield b"y" * 512
            yield b'"}'

        streamed = await client.post(hook_path(integration), content=_stream(), headers={"X-GitHub-Event": "push"})
        assert streamed.status_code == 413
        assert await _count(db_session, RepositoryDelivery) == 0

    async def test_repository_name_mismatch_is_ignored(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        resp = await _post(client, integration, push_payload("github", [commit("m", "RHOOK-1")], repo="someone/else"))
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored"}
        assert queued == []
        delivery = (await db_session.execute(select(RepositoryDelivery))).scalar_one()
        assert delivery.status == "ignored"
        assert "someone/else" in delivery.detail

    async def test_repository_name_compared_case_insensitively(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project, repo="Example-Org/Widgets")
        resp = await _post(client, integration, push_payload("github", [commit("m", "RHOOK-1")], repo=REPO))
        assert resp.status_code == 202

    async def test_invalid_json_is_400_and_logged(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        resp = await _post(client, integration, b"{not json")
        assert resp.status_code == 400
        delivery = (await db_session.execute(select(RepositoryDelivery))).scalar_one()
        assert delivery.status == "failed"

    async def test_unhandled_event_is_ignored(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        resp = await _post(client, integration, push_payload("github", [commit("e", "RHOOK-1")]), event="issues")
        assert (resp.status_code, resp.json()) == (200, {"status": "ignored"})
        delivery = (await db_session.execute(select(RepositoryDelivery))).scalar_one()
        assert (delivery.event, delivery.status) == ("issues", "ignored")

    async def test_cookie_without_csrf_token_is_not_refused(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        client.cookies.set("access_token", "stale-session")
        resp = await _post(client, integration, push_payload("github", [commit("c", "RHOOK-1")]))
        assert resp.status_code == 202


class TestPing:
    async def test_github_ping_marks_connected(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        resp = await _post(client, integration, fixture_payload("github_ping.json"), event="ping", delivery="p-1")
        assert (resp.status_code, resp.json()) == (200, {"status": "processed"})
        assert queued == []
        await db_session.refresh(integration)
        assert integration.last_delivery_at is not None
        delivery = (await db_session.execute(select(RepositoryDelivery))).scalar_one()
        assert (delivery.event, delivery.status) == ("ping", "processed")


@pytest.mark.parametrize("provider", ["gitlab", "gitea", "forgejo"])
class TestOtherProviders:
    async def test_push_links_like_github(self, client, db_session, world, queued, provider):
        integration = await make_integration(db_session, world.project, provider=provider)
        payload = push_payload(provider, [commit(provider, "Closes RHOOK-1")])
        await _push_and_process(client, db_session, integration, payload, queued, delivery=f"{provider}-1")
        link = (await db_session.execute(select(IssueCommitLink))).scalar_one()
        assert (link.issue_id, link.keyword) == (world.issues["RHOOK-1"].id, "fixes")
        (journal,) = await _journals(db_session, world.issues["RHOOK-1"])
        assert journal.user_id == (await _git_user(db_session)).id

    async def test_redelivery_is_a_duplicate(self, client, db_session, world, queued, provider):
        integration = await make_integration(db_session, world.project, provider=provider)
        payload = push_payload(provider, [commit(provider, "RHOOK-1")])
        first = await _post(client, integration, payload, delivery="same")
        second = await _post(client, integration, payload, delivery="same")
        assert (first.status_code, second.status_code, second.json()) == (202, 200, {"status": "duplicate"})
        assert await _count(db_session, RepositoryDelivery) == 1

    async def test_wrong_secret_is_401(self, client, db_session, world, queued, provider):
        integration = await make_integration(db_session, world.project, provider=provider)
        resp = await _post(client, integration, push_payload(provider, [commit(provider, "RHOOK-1")]), secret="nope")
        assert resp.status_code == 401
