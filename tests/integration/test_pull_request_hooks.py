"""Pull/merge request webhooks end to end: links, state, metadata, and merge automation as ``$git``.

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

from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.lookups import IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.repository import IssuePullRequestLink, PullRequest, RepositoryDelivery
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.workflow import WorkflowTransition
from specivo.services.repository_hooks import process_delivery
from specivo.services.workflow_service import WorkflowService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import (
    ClosedStatusFactory,
    DoneStatusFactory,
    PriorityFactory,
    StatusFactory,
    TrackerFactory,
)
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory
from tests.fixtures.forge_hooks import hook_path, make_integration, pull_request_payload, signed_headers

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@dataclass
class World:
    project: Project
    other: Project
    tracker: Tracker
    new: IssueStatus
    resolved: IssueStatus
    closed: IssueStatus
    issues: dict[str, Issue]


async def _issue(db: AsyncSession, project: Project, number: int, author: User, tracker, status, priority) -> Issue:
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
    tracker, priority = TrackerFactory.build(), PriorityFactory.build()
    new, resolved, closed = (
        StatusFactory.build(name="PR New"),
        DoneStatusFactory.build(name="PR Resolved"),
        (ClosedStatusFactory.build(name="PR Closed")),
    )
    db_session.add_all([tracker, priority, new, resolved, closed])
    author = UserFactory.build(login="prhook_author", status="active")
    project = ProjectFactory.build(key="PRH", identifier="prh", name="Pull requests", is_public=False)
    other = ProjectFactory.build(key="PRO", identifier="pro", name="Private other", is_public=False)
    db_session.add_all([author, project, other])
    await db_session.flush()
    issues = {}
    for n in (1, 2, 3):
        issues[f"PRH-{n}"] = await _issue(db_session, project, n, author, tracker, new, priority)
    issues["PRO-1"] = await _issue(db_session, other, 1, author, tracker, new, priority)
    await db_session.commit()
    return World(project, other, tracker, new, resolved, closed, issues)


@pytest.fixture
def queued(monkeypatch) -> list[int]:
    calls: list[int] = []

    def _record(ids):
        calls.extend(ids)
        return len(calls)

    monkeypatch.setattr("specivo.hooks.router.queue_processing", _record)
    return calls


async def _automation_role(db: AsyncSession) -> Role:
    return (await db.execute(select(Role).where(Role.name == "Automation"))).scalar_one()


async def _git(db: AsyncSession) -> User:
    return (await db.execute(select(User).where(User.login == "$git"))).scalar_one()


async def _grant_git(db: AsyncSession, project: Project, role: Role | None = None) -> Role:
    role = role or await _automation_role(db)
    member = Member(user_id=(await _git(db)).id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()
    return role


def _automation(world: World, **extra) -> dict:
    return {"merge_automation_enabled": True, "merge_status_id": world.resolved.id, **extra}


async def _send(client: AsyncClient, db: AsyncSession, integration, payload, queued, *, delivery=None):
    body = json.dumps(payload).encode()
    headers = signed_headers(integration.provider, body, event="pull_request", delivery=delivery or str(uuid.uuid4()))
    resp = await client.post(hook_path(integration), content=body, headers=headers)
    assert resp.status_code == 202, resp.text
    result = await process_delivery(db, queued[-1])
    await db.commit()
    return result


async def _journals(db: AsyncSession, issue: Issue) -> list[Journal]:
    rows = await db.execute(select(Journal).where(Journal.issue_id == issue.id).order_by(Journal.sequence))
    return list(rows.scalars().all())


async def _reload(db: AsyncSession, issue: Issue) -> Issue:
    await db.refresh(issue)
    return issue


async def _count(db: AsyncSession, model) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


class TestLinks:
    @pytest.mark.parametrize("provider", ["github", "gitlab", "gitea", "forgejo"])
    async def test_open_then_merged_updates_one_row(self, client, db_session, world, queued, provider):
        integration = await make_integration(db_session, world.project, provider=provider)
        opened = pull_request_payload(
            provider, title="Login form", body="Fixes PRH-1", branch="feat/PRH-2-x", updated_at="2026-10-01T09:00:00Z"
        )
        result = await _send(client, db_session, integration, opened, queued)
        assert result.status == "processed"

        pr = (await db_session.execute(select(PullRequest))).scalar_one()
        assert (pr.number, pr.state, pr.source_branch, pr.target_branch) == (7, "open", "feat/PRH-2-x", "main")
        links = {
            link.issue_id: (link.source, link.closing)
            for link in (await db_session.execute(select(IssuePullRequestLink))).scalars()
        }
        assert links == {world.issues["PRH-1"].id: ("body", True), world.issues["PRH-2"].id: ("branch", False)}

        merged = pull_request_payload(
            provider,
            title="Login form",
            body="Fixes PRH-1",
            branch="feat/PRH-2-x",
            state="merged",
            updated_at="2026-10-02T09:00:00Z",
        )
        await _send(client, db_session, integration, merged, queued)
        await db_session.refresh(pr)
        assert await _count(db_session, PullRequest) == 1
        assert await _count(db_session, IssuePullRequestLink) == 2
        assert pr.state == "merged"
        assert pr.merged_at is not None
        assert pr.merge_commit_sha == "c48d3ae2e3a7b7f0c3b1b2ad1d9e9b5d0f3a7e21"
        assert pr.merged_by_login == "sam-example"

        # One journal per newly linked issue, by $git; nothing more for the state change.
        git = await _git(db_session)
        (journal,) = await _journals(db_session, world.issues["PRH-1"])
        assert journal.user_id == git.id
        noun = "Merge request [!7]" if provider == "gitlab" else "Pull request [#7]"
        assert journal.notes.startswith(noun)
        assert "Login form" in journal.notes

        issue = await _reload(db_session, world.issues["PRH-1"])
        assert issue.issue_metadata["pull_requests"] == [pr.url]
        assert issue.issue_metadata["branches"] == ["feat/PRH-2-x"]
        assert issue.status_id == world.new.id  # automation is off by default

    async def test_metadata_is_appended_without_duplicates(self, client, db_session, world, queued):
        issue = world.issues["PRH-1"]
        issue.issue_metadata = {"pull_requests": ["https://forge.example.com/example-org/widgets/pull/7"]}
        await db_session.commit()
        integration = await make_integration(db_session, world.project)
        await _send(client, db_session, integration, pull_request_payload("github", title="PRH-1"), queued)
        await _send(
            client,
            db_session,
            integration,
            pull_request_payload("github", number=8, title="PRH-1 again", branch="feat/login-form"),
            queued,
        )
        issue = await _reload(db_session, issue)
        assert issue.issue_metadata["pull_requests"] == [
            "https://forge.example.com/example-org/widgets/pull/7",
            "https://forge.example.com/example-org/widgets/pull/8",
        ]
        assert issue.issue_metadata["branches"] == ["feat/login-form"]

    async def test_merged_before_an_older_edit_keeps_merged(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        merged = pull_request_payload("github", title="PRH-1 final", state="merged", updated_at="2026-10-02T09:00:00Z")
        older = pull_request_payload("github", title="PRH-1 draft title", updated_at="2026-10-01T09:00:00Z")
        await _send(client, db_session, integration, merged, queued)
        result = await _send(client, db_session, integration, older, queued)
        pr = (await db_session.execute(select(PullRequest))).scalar_one()
        assert (pr.state, pr.title) == ("merged", "PRH-1 final")
        assert "older" in result.detail.lower()

    async def test_reopen_after_close(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        for state, at in (("open", "01"), ("closed", "02"), ("open", "03")):
            payload = pull_request_payload("github", title="PRH-1", state=state, updated_at=f"2026-10-{at}T09:00:00Z")
            await _send(client, db_session, integration, payload, queued)
        pr = (await db_session.execute(select(PullRequest))).scalar_one()
        assert pr.state == "open"
        assert len(await _journals(db_session, world.issues["PRH-1"])) == 1

    async def test_out_of_scope_reference_is_ignored(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        payload = pull_request_payload("github", title="Fixes PRO-1", branch="feat/PRO-1-x")
        result = await _send(client, db_session, integration, payload, queued)
        assert result.status == "processed"
        assert await _count(db_session, PullRequest) == 0
        assert await _journals(db_session, world.issues["PRO-1"]) == []
        assert "PRO" not in result.detail

    async def test_redelivery_is_a_duplicate(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project)
        payload = pull_request_payload("github", title="PRH-1")
        await _send(client, db_session, integration, payload, queued, delivery="same")
        body = json.dumps(payload).encode()
        resp = await client.post(
            hook_path(integration),
            content=body,
            headers=signed_headers("github", body, event="pull_request", delivery="same"),
        )
        assert resp.json() == {"status": "duplicate"}
        assert len(await _journals(db_session, world.issues["PRH-1"])) == 1

    async def test_other_gitea_pull_request_events_are_ignored(self, client, db_session, world, queued):
        integration = await make_integration(db_session, world.project, provider="gitea")
        body = json.dumps(pull_request_payload("gitea", title="PRH-1")).encode()
        resp = await client.post(
            hook_path(integration), content=body, headers=signed_headers("gitea", body, event="pull_request_label")
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored"}


class TestMergeAutomation:
    async def test_merge_moves_closing_issue_as_git(self, client, db_session, world, queued):
        await _grant_git(db_session, world.project)
        integration = await make_integration(db_session, world.project, settings=_automation(world))
        payload = pull_request_payload("github", title="Login", body="Fixes PRH-1, refs PRH-2", state="merged")
        result = await _send(client, db_session, integration, payload, queued)

        assert (await _reload(db_session, world.issues["PRH-1"])).status_id == world.resolved.id
        assert (await _reload(db_session, world.issues["PRH-2"])).status_id == world.new.id  # not closing
        git = await _git(db_session)
        journals = await _journals(db_session, world.issues["PRH-1"])
        status_journal = journals[-1]
        assert status_journal.user_id == git.id
        assert "merged" in status_journal.notes and "sam-example" in status_journal.notes
        assert "PRH-1" in result.detail

        # The same merge delivered again (new delivery id) changes nothing.
        await _send(client, db_session, integration, payload, queued)
        assert len(await _journals(db_session, world.issues["PRH-1"])) == len(journals)

    async def test_automation_off_changes_nothing(self, client, db_session, world, queued):
        await _grant_git(db_session, world.project)
        integration = await make_integration(db_session, world.project, settings={"merge_status_id": world.resolved.id})
        payload = pull_request_payload("github", body="Fixes PRH-1", state="merged")
        await _send(client, db_session, integration, payload, queued)
        assert (await _reload(db_session, world.issues["PRH-1"])).status_id == world.new.id

    async def test_merge_into_another_branch_changes_nothing(self, client, db_session, world, queued):
        await _grant_git(db_session, world.project)
        integration = await make_integration(db_session, world.project, settings=_automation(world))
        payload = pull_request_payload("github", body="Fixes PRH-1", state="merged", target="release/1.0")
        await _send(client, db_session, integration, payload, queued)
        assert (await _reload(db_session, world.issues["PRH-1"])).status_id == world.new.id

    async def test_closed_without_merge_changes_nothing(self, client, db_session, world, queued):
        await _grant_git(db_session, world.project)
        integration = await make_integration(db_session, world.project, settings=_automation(world))
        payload = pull_request_payload("github", body="Fixes PRH-1", state="closed")
        await _send(client, db_session, integration, payload, queued)
        assert (await _reload(db_session, world.issues["PRH-1"])).status_id == world.new.id

    async def test_already_closed_issue_is_skipped(self, client, db_session, world, queued):
        world.issues["PRH-1"].status_id = world.closed.id
        await db_session.commit()
        await _grant_git(db_session, world.project)
        integration = await make_integration(db_session, world.project, settings=_automation(world))
        payload = pull_request_payload("github", body="Fixes PRH-1", state="merged")
        result = await _send(client, db_session, integration, payload, queued)
        assert (await _reload(db_session, world.issues["PRH-1"])).status_id == world.closed.id
        assert "already_closed" in result.detail

    async def test_workflow_refuses_then_allows_after_grant(self, client, db_session, world, queued):
        role = await _grant_git(db_session, world.project)
        # Workflow rules exist (for another role), none for Automation.
        developer = Role(name="PR Developer", permissions=["edit_issues"])
        db_session.add(developer)
        await db_session.flush()
        db_session.add(
            WorkflowTransition(
                tracker_id=world.tracker.id,
                role_id=developer.id,
                old_status_id=world.new.id,
                new_status_id=world.resolved.id,
            )
        )
        await db_session.commit()
        integration = await make_integration(db_session, world.project, settings=_automation(world))
        payload = pull_request_payload("github", body="Fixes PRH-1", state="merged")

        result = await _send(client, db_session, integration, payload, queued)
        issue = await _reload(db_session, world.issues["PRH-1"])
        assert issue.status_id == world.new.id
        assert "transition_not_allowed" in result.detail
        delivery = (
            (await db_session.execute(select(RepositoryDelivery).order_by(RepositoryDelivery.id))).scalars().all()
        )
        assert "transition_not_allowed" in delivery[-1].detail
        notes = [j.notes for j in await _journals(db_session, issue)]
        refusals = [n for n in notes if n and "could not move" in n]
        assert len(refusals) == 1
        assert "PR Resolved" in refusals[0] and "Automation" in refusals[0]

        db_session.add(
            WorkflowTransition(
                tracker_id=world.tracker.id,
                role_id=role.id,
                old_status_id=world.new.id,
                new_status_id=world.resolved.id,
            )
        )
        await db_session.commit()
        await WorkflowService().invalidate_cache()  # as the admin workflow editor does
        await _send(client, db_session, integration, payload, queued)
        assert (await _reload(db_session, issue)).status_id == world.resolved.id

    async def test_refused_merge_notes_the_issue_once(self, client, db_session, world, queued):
        await _grant_git(db_session, world.project)
        developer = Role(name="PR Note Developer", permissions=["edit_issues"])
        db_session.add(developer)
        await db_session.flush()
        db_session.add(
            WorkflowTransition(
                tracker_id=world.tracker.id,
                role_id=developer.id,
                old_status_id=world.new.id,
                new_status_id=world.resolved.id,
            )
        )
        await db_session.commit()
        integration = await make_integration(db_session, world.project, settings=_automation(world))

        # The merge, then later events on the merged request (labels, edits): each is a new delivery.
        for updated_at in ("2026-10-02T15:30:00Z", "2026-10-02T16:00:00Z", "2026-10-02T17:00:00Z"):
            payload = pull_request_payload("github", body="Fixes PRH-1", state="merged", updated_at=updated_at)
            result = await _send(client, db_session, integration, payload, queued)
            assert "transition_not_allowed" in result.detail

        issue = await _reload(db_session, world.issues["PRH-1"])
        assert issue.status_id == world.new.id
        notes = [j.notes for j in await _journals(db_session, issue)]
        assert len([n for n in notes if n and "could not move" in n]) == 1

    async def test_reference_added_after_the_merge_is_not_acted_on(self, client, db_session, world, queued):
        await _grant_git(db_session, world.project)
        integration = await make_integration(db_session, world.project, settings=_automation(world))
        merged = pull_request_payload("github", body="Fixes PRH-1", state="merged", updated_at="2026-10-02T15:30:00Z")
        await _send(client, db_session, integration, merged, queued)
        assert (await _reload(db_session, world.issues["PRH-1"])).status_id == world.resolved.id

        # Someone edits the merged request's text to claim another issue.
        edited = pull_request_payload(
            "github", body="Fixes PRH-1, fixes PRH-2", state="merged", updated_at="2026-10-02T16:00:00Z"
        )
        edited["action"] = "edited"
        await _send(client, db_session, integration, edited, queued)
        assert (await _reload(db_session, world.issues["PRH-2"])).status_id == world.new.id
        link = (
            await db_session.execute(
                select(IssuePullRequestLink).where(IssuePullRequestLink.issue_id == world.issues["PRH-2"].id)
            )
        ).scalar_one()
        assert link.closing is True  # recorded, never acted on

    @pytest.mark.parametrize("provider", ["github", "gitlab", "gitea", "forgejo"])
    async def test_edit_of_a_merged_request_never_stored_is_not_acted_on(
        self, client, db_session, world, queued, provider
    ):
        await _grant_git(db_session, world.project)
        integration = await make_integration(db_session, world.project, provider=provider, settings=_automation(world))
        # Merged while its text referenced nothing in scope.
        merged = pull_request_payload(provider, body="No issue", state="merged", updated_at="2026-10-02T15:30:00Z")
        await _send(client, db_session, integration, merged, queued)

        # Then the text is edited to claim an issue: the edit is no merge.
        edited = pull_request_payload(provider, body="Fixes PRH-1", state="merged", updated_at="2026-10-02T16:00:00Z")
        if provider == "gitlab":
            edited["object_attributes"]["action"] = "update"
        else:
            edited["action"] = "edited"
        result = await _send(client, db_session, integration, edited, queued)
        assert (await _reload(db_session, world.issues["PRH-1"])).status_id == world.new.id
        assert "Merge automation" not in result.detail

    @pytest.mark.parametrize("provider", ["github", "gitlab"])
    async def test_edit_event_first_seen_already_merged_is_not_acted_on(
        self, client, db_session, world, queued, provider
    ):
        # The merge itself was never delivered (the hook was added later); an edit arrives first.
        await _grant_git(db_session, world.project)
        integration = await make_integration(db_session, world.project, provider=provider, settings=_automation(world))
        edited = pull_request_payload(provider, body="Fixes PRH-1", state="merged")
        if provider == "gitlab":
            edited["object_attributes"]["action"] = "update"
        else:
            edited["action"] = "edited"
        await _send(client, db_session, integration, edited, queued)
        assert (await _reload(db_session, world.issues["PRH-1"])).status_id == world.new.id

    @pytest.mark.parametrize("provider", ["github", "gitlab"])
    @pytest.mark.parametrize(
        ("merge_body", "merge_time", "moved"),
        [
            # The merge delivery arrives late (older than the edit): its text decides.
            ("Fixes PRH-2", "2026-10-01T10:00:00Z", {"PRH-2"}),
            # The merge arrives after the edit and its text references the edited-in issue.
            ("Fixes PRH-1", "2026-10-01T12:00:00Z", {"PRH-1"}),
        ],
    )
    async def test_edit_showing_merged_before_the_merge_event_qualifies_nothing(
        self, client, db_session, world, queued, provider, merge_body, merge_time, moved
    ):
        await _grant_git(db_session, world.project)
        integration = await make_integration(db_session, world.project, provider=provider, settings=_automation(world))
        opened = pull_request_payload(provider, body="Refs PRH-3", updated_at="2026-10-01T09:00:00Z")
        await _send(client, db_session, integration, opened, queued)

        # The merge delivery is lost or late; an edit that already shows merged adds a fixing reference.
        edited = pull_request_payload(provider, body="Fixes PRH-1", state="merged", updated_at="2026-10-01T11:00:00Z")
        if provider == "gitlab":
            edited["object_attributes"]["action"] = "update"
        else:
            edited["action"] = "edited"
        await _send(client, db_session, integration, edited, queued)
        assert (await _reload(db_session, world.issues["PRH-1"])).status_id == world.new.id

        merge = pull_request_payload(provider, body=merge_body, state="merged", updated_at=merge_time)
        await _send(client, db_session, integration, merge, queued)
        for key in ("PRH-1", "PRH-2", "PRH-3"):
            expected = world.resolved.id if key in moved else world.new.id
            assert (await _reload(db_session, world.issues[key])).status_id == expected, key

    async def test_closing_upgrade_after_the_merge_is_not_acted_on(self, client, db_session, world, queued):
        await _grant_git(db_session, world.project)
        integration = await make_integration(db_session, world.project, settings=_automation(world))
        merged = pull_request_payload("github", body="Refs PRH-2", state="merged", updated_at="2026-10-02T15:30:00Z")
        await _send(client, db_session, integration, merged, queued)
        edited = pull_request_payload("github", body="Fixes PRH-2", state="merged", updated_at="2026-10-02T16:00:00Z")
        edited["action"] = "edited"
        await _send(client, db_session, integration, edited, queued)
        assert (await _reload(db_session, world.issues["PRH-2"])).status_id == world.new.id

    async def test_reference_dropped_before_the_merge_loses_closing(self, client, db_session, world, queued):
        await _grant_git(db_session, world.project)
        integration = await make_integration(db_session, world.project, settings=_automation(world))
        opened = pull_request_payload("github", body="Fixes PRH-1", updated_at="2026-10-01T09:00:00Z")
        await _send(client, db_session, integration, opened, queued)
        edited = pull_request_payload("github", body="Nothing to fix", updated_at="2026-10-01T10:00:00Z")
        await _send(client, db_session, integration, edited, queued)
        link = (
            await db_session.execute(
                select(IssuePullRequestLink).where(IssuePullRequestLink.issue_id == world.issues["PRH-1"].id)
            )
        ).scalar_one()
        assert link.closing is False

        merged = pull_request_payload(
            "github", body="Nothing to fix", state="merged", updated_at="2026-10-01T11:00:00Z"
        )
        await _send(client, db_session, integration, merged, queued)
        assert (await _reload(db_session, world.issues["PRH-1"])).status_id == world.new.id

    async def test_issue_that_left_the_scope_is_not_moved(self, client, db_session, world, queued):
        admin = AdminUserFactory.build(login="pr_scope_admin", email="pr-scope-admin@example.com", status="active")
        child = ProjectFactory.build(
            key="PRS", identifier="prs", name="Child", parent_id=world.project.id, path="prh.prs"
        )
        db_session.add_all([admin, child])
        await db_session.flush()
        source = world.issues["PRH-1"]
        child_issue = IssueFactory.build(
            project_id=child.id,
            project_key="PRS",
            sequence_number=1,
            tracker_id=source.tracker_id,
            status_id=world.new.id,
            priority_id=source.priority_id,
            author_id=source.author_id,
        )
        db_session.add(child_issue)
        await db_session.commit()
        integration = await make_integration(
            db_session,
            world.project,
            scope="project_and_subprojects",
            created_by_id=admin.id,
            settings=_automation(world),
        )
        payload = pull_request_payload("github", body="Fixes PRS-1", state="merged")
        await _send(client, db_session, integration, payload, queued)
        link = (
            await db_session.execute(
                select(IssuePullRequestLink).where(IssuePullRequestLink.issue_id == child_issue.id)
            )
        ).scalar_one()
        # Qualified at the merge, but $git had no role in the child project, so not handled.
        assert link.merge_qualified_at is not None and link.merge_handled_at is None

        integration.scope = "project"
        await db_session.commit()
        await _grant_git(db_session, child)
        result = await _send(client, db_session, integration, payload, queued)
        assert (await _reload(db_session, child_issue)).status_id == world.new.id
        assert "out_of_scope" in result.detail

    async def test_private_issue_git_cannot_see_is_never_touched(self, client, db_session, world, queued):
        await _grant_git(db_session, world.project)
        issue = world.issues["PRH-1"]
        issue.is_private = True
        await db_session.commit()
        integration = await make_integration(db_session, world.project, settings=_automation(world))
        payload = pull_request_payload("github", body="Fixes PRH-1", state="merged")
        result = await _send(client, db_session, integration, payload, queued)

        assert (await _reload(db_session, issue)).status_id == world.new.id
        assert "issue_not_visible" in result.detail
        notes = [j.notes or "" for j in await _journals(db_session, issue)]
        assert not any("merged into" in n for n in notes)

    async def test_git_without_membership_does_nothing(self, client, db_session, world, queued):
        world.project.is_public = True  # the Non member role must not stand in for a membership
        await db_session.commit()
        integration = await make_integration(db_session, world.project, settings=_automation(world))
        payload = pull_request_payload("github", body="Fixes PRH-1", state="merged")
        result = await _send(client, db_session, integration, payload, queued)
        issue = await _reload(db_session, world.issues["PRH-1"])
        assert issue.status_id == world.new.id
        assert "no_git_membership" in result.detail
        assert not any(j.notes and "could not move" in j.notes for j in await _journals(db_session, issue))

    async def test_role_without_edit_issues_does_nothing(self, client, db_session, world, queued):
        viewer = Role(name="PR Viewer", permissions=["view_issues"])
        db_session.add(viewer)
        await db_session.flush()
        await _grant_git(db_session, world.project, viewer)
        integration = await make_integration(db_session, world.project, settings=_automation(world))
        payload = pull_request_payload("github", body="Fixes PRH-1", state="merged")
        result = await _send(client, db_session, integration, payload, queued)
        assert (await _reload(db_session, world.issues["PRH-1"])).status_id == world.new.id
        assert "missing_permission:edit_issues" in result.detail

    async def test_admin_author_gains_no_rights(self, client, db_session, world, queued):
        admin = AdminUserFactory.build(login="pr_admin", email="pr-admin@example.com", status="active")
        db_session.add(admin)
        await _grant_git(db_session, world.project)
        developer = Role(name="PR Developer 2", permissions=["edit_issues"])
        db_session.add(developer)
        await db_session.flush()
        db_session.add(
            WorkflowTransition(
                tracker_id=world.tracker.id,
                role_id=developer.id,
                old_status_id=world.new.id,
                new_status_id=world.resolved.id,
            )
        )
        await db_session.commit()
        integration = await make_integration(db_session, world.project, settings=_automation(world))
        payload = pull_request_payload(
            "github", body="Fixes PRH-1", state="merged", author="pr_admin", merged_by="pr_admin"
        )
        await _send(client, db_session, integration, payload, queued)
        assert (await _reload(db_session, world.issues["PRH-1"])).status_id == world.new.id


ALL_PROVIDERS = ["github", "gitlab", "gitea", "forgejo"]


class TestExternalPullRequests:
    """``ignore_external_pull_requests`` (on by default) skips requests from forks and outsiders."""

    async def _links(self, db: AsyncSession) -> int:
        return int(await db.scalar(select(func.count()).select_from(IssuePullRequestLink)) or 0)

    async def _last_delivery(self, db: AsyncSession) -> RepositoryDelivery:
        rows = await db.execute(select(RepositoryDelivery).order_by(RepositoryDelivery.id.desc()).limit(1))
        return rows.scalar_one()

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    async def test_fork_skipped_by_default(self, client, db_session, world, queued, provider):
        integration = await make_integration(db_session, world.project, provider=provider)
        payload = pull_request_payload(provider, body="Fixes PRH-1", external=True)
        result = await _send(client, db_session, integration, payload, queued)
        assert result.status == "ignored"
        assert "external_pull_request" in result.detail
        delivery = await self._last_delivery(db_session)
        assert (delivery.status, "external_pull_request" in (delivery.detail or "")) == ("ignored", True)
        assert await self._links(db_session) == 0
        assert await db_session.scalar(select(func.count()).select_from(PullRequest)) == 0
        assert await _journals(db_session, world.issues["PRH-1"]) == []

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    async def test_fork_processed_when_off(self, client, db_session, world, queued, provider):
        integration = await make_integration(
            db_session, world.project, provider=provider, settings={"ignore_external_pull_requests": False}
        )
        payload = pull_request_payload(provider, body="Fixes PRH-1", external=True)
        result = await _send(client, db_session, integration, payload, queued)
        assert result.status == "processed"
        assert await self._links(db_session) == 1

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    async def test_same_repository_processed_when_on(self, client, db_session, world, queued, provider):
        integration = await make_integration(
            db_session, world.project, provider=provider, settings={"ignore_external_pull_requests": True}
        )
        result = await _send(
            client, db_session, integration, pull_request_payload(provider, body="Fixes PRH-1"), queued
        )
        assert result.status == "processed"
        assert await self._links(db_session) == 1

    @pytest.mark.parametrize("association", ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE"])
    async def test_github_author_without_a_role_skipped(self, client, db_session, world, queued, association):
        integration = await make_integration(db_session, world.project)
        payload = pull_request_payload("github", body="Fixes PRH-1", author_association=association)
        result = await _send(client, db_session, integration, payload, queued)
        assert result.status == "ignored"
        assert "external_pull_request" in result.detail
        assert await self._links(db_session) == 0

    @pytest.mark.parametrize("association", ["MEMBER", "COLLABORATOR"])
    async def test_github_collaborator_processed(self, client, db_session, world, queued, association):
        integration = await make_integration(db_session, world.project)
        payload = pull_request_payload("github", body="Fixes PRH-1", author_association=association)
        result = await _send(client, db_session, integration, payload, queued)
        assert result.status == "processed"
        assert await self._links(db_session) == 1

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    async def test_merging_a_fork_still_links_and_runs_automation(self, client, db_session, world, queued, provider):
        """Only someone with write access merges: the merge itself is never noise."""
        await _grant_git(db_session, world.project)
        integration = await make_integration(db_session, world.project, provider=provider, settings=_automation(world))
        opened = pull_request_payload(provider, body="Fixes PRH-1", external=True, updated_at="2026-10-01T09:00:00Z")
        assert (await _send(client, db_session, integration, opened, queued)).status == "ignored"
        merged = pull_request_payload(
            provider, body="Fixes PRH-1", external=True, state="merged", updated_at="2026-10-02T15:30:00Z"
        )
        result = await _send(client, db_session, integration, merged, queued)
        assert result.status == "processed"
        assert await self._links(db_session) == 1
        assert (await _reload(db_session, world.issues["PRH-1"])).status_id == world.resolved.id
        assert len(await _journals(db_session, world.issues["PRH-1"])) == 2  # the link note and the move

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    async def test_payload_without_repository_ids_is_skipped_not_failed(
        self, client, db_session, world, queued, provider
    ):
        """A payload that cannot show where the request comes from is treated as external."""
        integration = await make_integration(db_session, world.project, provider=provider)
        payload = pull_request_payload(provider, body="Fixes PRH-1")
        if provider == "gitlab":
            payload["object_attributes"].pop("source_project_id")
        else:
            payload["pull_request"]["head"] = "unexpected"
        result = await _send(client, db_session, integration, payload, queued)
        assert result.status == "ignored"
        assert "external_pull_request" in result.detail
        assert await self._links(db_session) == 0

    async def test_setting_defaults_to_on(self, client, db_session, world):
        from specivo.schemas.repository import RepositorySettings

        assert RepositorySettings().ignore_external_pull_requests is True
        assert RepositorySettings.model_validate({}, strict=False).ignore_external_pull_requests is True
