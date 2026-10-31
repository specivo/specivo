"""Processing repository deliveries (``specivo.services.repository_hooks``) without the HTTP layer."""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import REPOSITORY_MAX_REFS_PER_COMMIT
from specivo.core.utils import utcnow
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.member import Member, MemberRole
from specivo.models.repository import IssueCommitLink, RepositoryCommit, RepositoryDelivery
from specivo.models.role import Role
from specivo.services import repository_hooks
from specivo.services.repository_hooks import (
    claim_stale,
    process_delivery,
    purge_old_deliveries,
    queue_processing,
    record_delivery,
    scope_project_keys,
)
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory
from tests.fixtures.forge_hooks import commit, make_integration, push_payload

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]


@pytest_asyncio.fixture
async def setup(db_session: AsyncSession):
    lookups = (TrackerFactory.build(), StatusFactory.build(), PriorityFactory.build())
    db_session.add_all(lookups)
    author = UserFactory.build(login="rproc_author", status="active")
    parent = ProjectFactory.build(key="RPROC", identifier="rproc", name="Processing", path="rproc")
    db_session.add_all([author, parent])
    await db_session.flush()
    child = ProjectFactory.build(key="RPROD", identifier="rprod", name="Child", parent_id=parent.id, path="rproc.rprod")
    # A sibling whose path shares the prefix text but is not below the parent.
    lookalike = ProjectFactory.build(key="RPROE", identifier="rproc-x", name="Lookalike", path="rproc_x")
    db_session.add_all([child, lookalike])
    await db_session.flush()
    issues = {}
    for project in (parent, child, lookalike):
        issue = IssueFactory.build(
            project_id=project.id,
            project_key=project.key,
            sequence_number=1,
            tracker_id=lookups[0].id,
            status_id=lookups[1].id,
            priority_id=lookups[2].id,
            author_id=author.id,
        )
        db_session.add(issue)
        issues[project.key] = issue
    await db_session.commit()
    return {"parent": parent, "child": child, "lookalike": lookalike, "issues": issues}


async def _grant(db: AsyncSession, user, project, permissions: list[str]) -> None:
    role = Role(name=f"rproc-{user.login}-{project.key}", permissions=permissions)
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()


async def _deliver(db: AsyncSession, integration, payload, delivery_id="d-1"):
    row_id = await record_delivery(
        db, integration, delivery_id=delivery_id, event="push", status="accepted", payload=payload
    )
    await db.commit()
    result = await process_delivery(db, row_id)
    await db.commit()
    return row_id, result


class TestAttribution:
    async def test_verified_email_sets_matched_user(self, db_session, setup):
        user = UserFactory.build(login="rproc_dev", email="Dev@Example.com", email_verified_at=utcnow())
        db_session.add(user)
        await db_session.commit()
        integration = await make_integration(db_session, setup["parent"])
        await _deliver(
            db_session, integration, push_payload("github", [commit("v", "RPROC-1", email="dev@example.COM")])
        )
        stored = (await db_session.execute(select(RepositoryCommit))).scalar_one()
        assert stored.matched_user_id == user.id

    async def test_unverified_email_does_not(self, db_session, setup):
        user = UserFactory.build(login="rproc_unverified", email="dev2@example.com", email_verified_at=None)
        db_session.add(user)
        await db_session.commit()
        integration = await make_integration(db_session, setup["parent"])
        await _deliver(
            db_session, integration, push_payload("github", [commit("u", "RPROC-1", email="dev2@example.com")])
        )
        stored = (await db_session.execute(select(RepositoryCommit))).scalar_one()
        assert stored.matched_user_id is None

    async def test_journal_is_by_git_even_when_the_author_matches(self, db_session, setup):
        user = UserFactory.build(login="rproc_dev3", email="dev3@example.com", email_verified_at=utcnow())
        db_session.add(user)
        await db_session.commit()
        integration = await make_integration(db_session, setup["parent"])
        await _deliver(
            db_session, integration, push_payload("github", [commit("j", "RPROC-1", email="dev3@example.com")])
        )
        journal = (await db_session.execute(select(Journal))).scalar_one()
        git = await repository_hooks.get_git_user(db_session)
        assert journal.user_id == git.id != user.id


class TestScope:
    async def test_project_scope_is_the_project_only(self, db_session, setup):
        integration = await make_integration(db_session, setup["parent"])
        assert await scope_project_keys(db_session, integration) == {"RPROC": setup["parent"].id}

    async def test_subproject_scope_adds_descendants_only(self, db_session, setup):
        admin = AdminUserFactory.build(login="rproc_admin")
        db_session.add(admin)
        await db_session.commit()
        integration = await make_integration(
            db_session, setup["parent"], scope="project_and_subprojects", created_by_id=admin.id
        )
        assert await scope_project_keys(db_session, integration) == {
            "RPROC": setup["parent"].id,
            "RPROD": setup["child"].id,
        }

    async def test_subproject_scope_needs_the_creator_to_manage_each_subproject(self, db_session, setup):
        # A manager of the parent only must not reach a subproject they do not manage.
        manager = UserFactory.build(login="rproc_manager", status="active")
        db_session.add(manager)
        await db_session.flush()
        await _grant(db_session, manager, setup["parent"], ["view_issues", "manage_project"])
        integration = await make_integration(
            db_session, setup["parent"], scope="project_and_subprojects", created_by_id=manager.id
        )
        assert await scope_project_keys(db_session, integration) == {"RPROC": setup["parent"].id}

        await _grant(db_session, manager, setup["child"], ["view_issues", "manage_project"])
        db_session.info.clear()  # roles are cached per session; each delivery runs in a fresh one
        assert await scope_project_keys(db_session, integration) == {
            "RPROC": setup["parent"].id,
            "RPROD": setup["child"].id,
        }

    async def test_a_manager_who_reconfigures_an_admins_integration_gets_their_own_scope(self, db_session, setup):
        # Rotating or editing must not borrow the administrator's rights over subprojects.
        from specivo.services.repository_service import RepositoryIntegrationService

        admin = AdminUserFactory.build(login="rproc_admin2")
        manager = UserFactory.build(login="rproc_manager2", status="active")
        db_session.add_all([admin, manager])
        await db_session.flush()
        await _grant(db_session, manager, setup["parent"], ["view_issues", "manage_project"])
        integration = await make_integration(
            db_session, setup["parent"], scope="project_and_subprojects", created_by_id=admin.id
        )
        assert set(await scope_project_keys(db_session, integration)) == {"RPROC", "RPROD"}

        await RepositoryIntegrationService().rotate_secret(
            db_session, integration.id, setup["parent"].id, actor=manager
        )
        await db_session.commit()
        assert set(await scope_project_keys(db_session, integration)) == {"RPROC"}

        await RepositoryIntegrationService().rotate_secret(db_session, integration.id, setup["parent"].id, actor=admin)
        await db_session.commit()
        assert set(await scope_project_keys(db_session, integration)) == {"RPROC", "RPROD"}

    async def test_subproject_scope_without_a_creator_is_the_project_only(self, db_session, setup):
        integration = await make_integration(db_session, setup["parent"], scope="project_and_subprojects")
        assert await scope_project_keys(db_session, integration) == {"RPROC": setup["parent"].id}

    async def test_child_integration_does_not_reach_its_parent(self, db_session, setup):
        integration = await make_integration(db_session, setup["child"], scope="project_and_subprojects")
        await _deliver(db_session, integration, push_payload("github", [commit("p", "RPROC-1 and RPROD-1")]))
        links = (await db_session.execute(select(IssueCommitLink))).scalars().all()
        assert [link.issue_id for link in links] == [setup["issues"]["RPROD"].id]


class TestOutcomes:
    async def test_disabled_integration_is_ignored(self, db_session, setup):
        integration = await make_integration(db_session, setup["parent"])
        row_id = await record_delivery(
            db_session,
            integration,
            delivery_id="d-off",
            event="push",
            status="accepted",
            payload=push_payload("github", [commit("x", "RPROC-1")]),
        )
        integration.is_active = False
        await db_session.commit()
        result = await process_delivery(db_session, row_id)
        assert result.status == "ignored"
        assert (await db_session.scalar(select(func.count()).select_from(IssueCommitLink))) == 0

    async def test_failure_rolls_back_and_is_recorded(self, db_session, setup, monkeypatch):
        integration = await make_integration(db_session, setup["parent"])

        async def _boom(session):
            raise RuntimeError("no git user")

        monkeypatch.setattr(repository_hooks, "get_git_user", _boom)
        row_id, result = await _deliver(db_session, integration, push_payload("github", [commit("f", "RPROC-1")]))
        assert result.status == "failed"
        delivery = await db_session.get(RepositoryDelivery, row_id)
        await db_session.refresh(delivery)
        assert (delivery.status, delivery.detail) == ("failed", "Processing failed: RuntimeError")
        assert delivery.processed_at is not None
        assert (await db_session.scalar(select(func.count()).select_from(IssueCommitLink))) == 0
        assert (await db_session.scalar(select(func.count()).select_from(RepositoryCommit))) == 0

    async def test_large_push_is_batched(self, db_session, setup, monkeypatch):
        monkeypatch.setattr(repository_hooks, "REPOSITORY_COMMIT_BATCH", 7)
        integration = await make_integration(db_session, setup["parent"])
        commits = [commit(f"c{i}", f"Step {i} for RPROC-1") for i in range(30)]
        _row, result = await _deliver(db_session, integration, push_payload("github", commits))
        assert result.status == "processed"
        assert len(result.new_links) == 30
        issue = await db_session.get(Issue, setup["issues"]["RPROC"].id)
        await db_session.refresh(issue)
        assert issue.issue_metadata["commits"] == [c["id"] for c in commits]

    async def test_long_message_is_capped(self, db_session, setup):
        integration = await make_integration(db_session, setup["parent"])
        await _deliver(db_session, integration, push_payload("github", [commit("l", "RPROC-1\n" + "z" * 20_000)]))
        stored = (await db_session.execute(select(RepositoryCommit))).scalar_one()
        assert len(stored.message) == 10_000


class TestSweepAndRetention:
    async def test_claim_stale_returns_old_accepted_rows_only(self, db_session, setup):
        integration = await make_integration(db_session, setup["parent"])
        now = utcnow()
        rows = [
            RepositoryDelivery(
                integration_id=integration.id,
                delivery_id="old",
                event="push",
                status="accepted",
                received_at=now - timedelta(minutes=10),
            ),
            RepositoryDelivery(
                integration_id=integration.id, delivery_id="new", event="push", status="accepted", received_at=now
            ),
            RepositoryDelivery(
                integration_id=integration.id,
                delivery_id="done",
                event="push",
                status="processed",
                received_at=now - timedelta(minutes=10),
            ),
        ]
        db_session.add_all(rows)
        await db_session.commit()
        assert await claim_stale(db_session) == [rows[0].id]

    async def test_purge_drops_rows_past_retention(self, db_session, setup):
        integration = await make_integration(db_session, setup["parent"])
        now = utcnow()
        db_session.add_all(
            [
                RepositoryDelivery(
                    integration_id=integration.id,
                    delivery_id="ancient",
                    event="ping",
                    status="processed",
                    received_at=now - timedelta(days=31),
                ),
                RepositoryDelivery(
                    integration_id=integration.id,
                    delivery_id="recent",
                    event="ping",
                    status="processed",
                    received_at=now - timedelta(days=29),
                ),
            ]
        )
        await db_session.commit()
        assert await purge_old_deliveries(db_session) == 1
        remaining = (await db_session.execute(select(RepositoryDelivery.delivery_id))).scalars().all()
        assert remaining == ["recent"]

    async def test_queue_processing_survives_a_dead_broker(self, monkeypatch):
        from specivo.tasks.repositories import process_repository_delivery

        def _down(*_args, **_kwargs):
            raise ConnectionError("broker down")

        monkeypatch.setattr(process_repository_delivery, "apply_async", _down)
        assert queue_processing([1, 2]) == 0

    async def test_queue_processing_queues(self, monkeypatch):
        from specivo.tasks.repositories import process_repository_delivery

        sent = []
        monkeypatch.setattr(process_repository_delivery, "apply_async", lambda **kw: sent.append(kw["args"]))
        assert queue_processing([5, 6]) == 2
        assert sent == [[5], [6]]


class TestLimits:
    async def test_references_past_the_per_commit_cap_are_ignored(self, db_session, setup):
        integration = await make_integration(db_session, setup["parent"])
        others = " ".join(f"RPROC-{n}" for n in range(2, 2 + REPOSITORY_MAX_REFS_PER_COMMIT))
        await _deliver(db_session, integration, push_payload("github", [commit("cap", f"{others} RPROC-1")]))
        assert (await db_session.execute(select(func.count()).select_from(IssueCommitLink))).scalar_one() == 0

        last_allowed = " ".join(f"RPROC-{n}" for n in range(2, 1 + REPOSITORY_MAX_REFS_PER_COMMIT))
        await _deliver(
            db_session, integration, push_payload("github", [commit("cap2", f"{last_allowed} RPROC-1")]), "d-2"
        )
        assert (await db_session.execute(select(func.count()).select_from(IssueCommitLink))).scalar_one() == 1

    async def test_redelivery_of_a_failed_delivery_is_accepted_again(self, db_session, setup):
        integration = await make_integration(db_session, setup["parent"])
        payload = push_payload("github", [commit("again", "RPROC-1")])
        first = await record_delivery(
            db_session, integration, delivery_id="d-f", event="push", status="accepted", payload=payload
        )
        await db_session.commit()
        row = await db_session.get(RepositoryDelivery, first)
        row.status = "failed"
        await db_session.commit()

        again = await record_delivery(
            db_session, integration, delivery_id="d-f", event="push", status="accepted", payload=payload
        )
        await db_session.commit()
        assert again == first
        await db_session.refresh(row)
        assert row.status == "accepted"

        # A processed delivery stays a duplicate.
        await process_delivery(db_session, first)
        await db_session.commit()
        assert (
            await record_delivery(
                db_session, integration, delivery_id="d-f", event="push", status="accepted", payload=payload
            )
            is None
        )
