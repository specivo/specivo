"""References under a renamed project's old key, or an issue's reference from before a move.

Commits and pull requests resolve them like the web UI does
(``project_key_aliases`` and ``issue_ref_aliases``), within the
integration's scope only: an old reference that now names an issue outside
the scope links nothing and says nothing about it.

Celery is not running here: the endpoint's queueing is replaced by a recorder
and the tests run ``process_delivery`` on the test session themselves.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue, IssueRefAlias
from specivo.models.journal import Journal
from specivo.models.project import Project, ProjectKeyAlias
from specivo.models.repository import IssueCommitLink, IssuePullRequestLink, RepositoryIntegration
from specivo.models.user import User
from specivo.services.repository_hooks import process_delivery
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory
from tests.fixtures.forge_hooks import (
    commit,
    hook_path,
    make_integration,
    pull_request_payload,
    push_payload,
    signed_headers,
)

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@dataclass
class World:
    project: Project
    child: Project
    outside: Project
    admin: User
    issues: dict[str, Issue]


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    """``RALI`` (renamed from ``RALO``) with a child ``RALC``, and ``RALX`` outside any scope.

    ``RALC-1`` was ``RALI-5`` before it moved to the child; ``RALX-1`` was
    ``RALI-9`` before it moved out.
    """
    tracker, status, priority = TrackerFactory.build(), StatusFactory.build(), PriorityFactory.build()
    admin = AdminUserFactory.build(login="ralias_admin")
    db_session.add_all([tracker, status, priority, admin])
    project = ProjectFactory.build(key="RALI", identifier="rali", name="Aliases", is_public=False)
    outside = ProjectFactory.build(key="RALX", identifier="ralx", name="Outside", is_public=False)
    db_session.add_all([project, outside])
    await db_session.flush()
    child = ProjectFactory.build(
        key="RALC", identifier="ralc", name="Alias child", parent_id=project.id, path=f"{project.path}.ralc"
    )
    db_session.add(child)
    await db_session.flush()
    issues: dict[str, Issue] = {}
    for p, number in ((project, 1), (child, 1), (outside, 1)):
        issue = IssueFactory.build(
            project_id=p.id,
            project_key=p.key,
            sequence_number=number,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
            author_id=admin.id,
            subject=f"{p.key} issue",
        )
        db_session.add(issue)
        await db_session.flush()
        issues[issue.display_key] = issue
    db_session.add_all(
        [
            ProjectKeyAlias(old_key="RALO", project_id=project.id, renamed_by_id=admin.id),
            IssueRefAlias(old_project_key="RALI", old_sequence_number=5, issue_id=issues["RALC-1"].id),
            IssueRefAlias(old_project_key="RALI", old_sequence_number=9, issue_id=issues["RALX-1"].id),
        ]
    )
    await db_session.commit()
    return World(project, child, outside, admin, issues)


@pytest.fixture
def queued(monkeypatch) -> list[int]:
    calls: list[int] = []

    def _record(ids):
        calls.extend(ids)
        return len(calls)

    monkeypatch.setattr("specivo.hooks.router.queue_processing", _record)
    return calls


async def _integration(db: AsyncSession, world: World) -> RepositoryIntegration:
    return await make_integration(db, world.project, scope="project_and_subprojects", created_by_id=world.admin.id)


async def _deliver(client, db: AsyncSession, integration, payload, queued, *, event: str):
    body = json.dumps(payload).encode()
    headers = signed_headers(integration.provider, body, event=event, delivery=str(uuid.uuid4()))
    resp = await client.post(hook_path(integration), content=body, headers=headers)
    assert resp.status_code == 202, resp.text
    result = await process_delivery(db, queued[-1])
    await db.commit()
    return result


async def _commit_links(db: AsyncSession) -> set[int]:
    return set((await db.execute(select(IssueCommitLink.issue_id))).scalars())


async def _pr_links(db: AsyncSession) -> dict[int, bool]:
    rows = await db.execute(select(IssuePullRequestLink.issue_id, IssuePullRequestLink.closing))
    return {issue_id: closing for issue_id, closing in rows.all()}


async def _journal_count(db: AsyncSession, issue: Issue) -> int:
    return len((await db.execute(select(Journal.id).where(Journal.issue_id == issue.id))).all())


class TestRenamedProject:
    async def test_commit_under_the_old_key(self, client, db_session, world, queued):
        integration = await _integration(db_session, world)
        payload = push_payload("github", [commit("r1", "Tidy the form, refs RALO-1")])
        result = await _deliver(client, db_session, integration, payload, queued, event="push")
        assert await _commit_links(db_session) == {world.issues["RALI-1"].id}
        assert "1 new link(s) to 1 issue(s)" in result.detail
        assert await _journal_count(db_session, world.issues["RALI-1"]) == 1

    async def test_pull_request_under_the_old_key(self, client, db_session, world, queued):
        integration = await _integration(db_session, world)
        payload = pull_request_payload("github", body="Closes RALO-1")
        await _deliver(client, db_session, integration, payload, queued, event="pull_request")
        assert await _pr_links(db_session) == {world.issues["RALI-1"].id: True}

    async def test_old_and_current_key_together_make_one_link(self, client, db_session, world, queued):
        integration = await _integration(db_session, world)
        payload = pull_request_payload("github", body="Refs RALO-1, closes RALI-1")
        await _deliver(client, db_session, integration, payload, queued, event="pull_request")
        assert await _pr_links(db_session) == {world.issues["RALI-1"].id: True}
        assert await _journal_count(db_session, world.issues["RALI-1"]) == 1

    async def test_old_and_current_key_in_one_commit_make_one_link(self, client, db_session, world, queued):
        integration = await _integration(db_session, world)
        payload = push_payload("github", [commit("r3", "Refs RALO-1 and RALI-1")])
        result = await _deliver(client, db_session, integration, payload, queued, event="push")
        assert await _commit_links(db_session) == {world.issues["RALI-1"].id}
        assert "1 new link(s) to 1 issue(s)" in result.detail
        assert await _journal_count(db_session, world.issues["RALI-1"]) == 1

    async def test_branch_name_under_the_old_key(self, client, db_session, world, queued):
        integration = await _integration(db_session, world)
        payload = push_payload("github", [commit("r2", "Tidy the form")], branch="RALO-1-tidy-form")
        await _deliver(client, db_session, integration, payload, queued, event="push")
        assert await _commit_links(db_session) == {world.issues["RALI-1"].id}


class TestMovedIssue:
    async def test_moved_within_the_scope_commit(self, client, db_session, world, queued):
        integration = await _integration(db_session, world)
        payload = push_payload("github", [commit("m1", "Fixes RALI-5")])
        await _deliver(client, db_session, integration, payload, queued, event="push")
        assert await _commit_links(db_session) == {world.issues["RALC-1"].id}

    async def test_moved_within_the_scope_pull_request(self, client, db_session, world, queued):
        integration = await _integration(db_session, world)
        payload = pull_request_payload("github", body="Fixes RALI-5")
        await _deliver(client, db_session, integration, payload, queued, event="pull_request")
        assert await _pr_links(db_session) == {world.issues["RALC-1"].id: True}

    async def test_moved_out_of_the_scope_links_nothing(self, client, db_session, world, queued):
        integration = await _integration(db_session, world)
        moved_out = await _deliver(
            client,
            db_session,
            integration,
            push_payload("github", [commit("m2", "Fixes RALI-9")]),
            queued,
            event="push",
        )
        missing = await _deliver(
            client,
            db_session,
            integration,
            push_payload("github", [commit("m3", "Fixes RALI-77")]),
            queued,
            event="push",
        )
        assert await _commit_links(db_session) == set()
        assert await _journal_count(db_session, world.issues["RALX-1"]) == 0
        # Exactly what a reference to an issue that never existed gets.
        assert moved_out.detail == missing.detail
        assert "RALX" not in moved_out.detail

    async def test_moved_out_of_the_scope_pull_request_links_nothing(self, client, db_session, world, queued):
        integration = await _integration(db_session, world)
        result = await _deliver(
            client,
            db_session,
            integration,
            pull_request_payload("github", body="Fixes RALI-9"),
            queued,
            event="pull_request",
        )
        assert await _pr_links(db_session) == {}
        assert await _journal_count(db_session, world.issues["RALX-1"]) == 0
        assert "RALX" not in result.detail
