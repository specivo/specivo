"""Full link graph rebuilds from project settings, and the graph's figures.

- ``POST /projects/{key}/links/rebuild/``: manage_project only, one run per
  project (409 while one runs, 503 when the run lock cannot be taken), a stale
  lock left by a finished task is cleared;
- the task: releases its Redis lock on success and on failure, and does
  nothing (keeping the lock) when another run holds the project's advisory
  lock;
- ``run_full_rebuild``: the expected links for a fixture project;
- ``GET /projects/{key}/links/stats/``: exact counts, unresolved targets only
  from texts the viewer may read, manage_project only.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from specivo.core.locks import LockLostError, LockUnavailableError, TokenLock
from specivo.core.redis import get_redis
from specivo.models.relation import IssueRelation
from specivo.models.user import User
from specivo.services.link_graph_admin_service import (
    LINK_REBUILD_LOCK_NAMESPACE,
    last_result,
    rebuild_lock_key,
    run_full_rebuild,
)
from specivo.tasks.entity_links import _full_rebuild_async, rebuild_link_graph_task
from tests.factories.user import TEST_PASSWORD, UserFactory
from tests.integration.link_graph_helpers import World, build_world, comment, grant, issue, page, rebuild

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "ELR")


@pytest.fixture
async def manager(db_session: AsyncSession, world: World) -> User:
    user = UserFactory.build(login="elr_manager", status="active")
    db_session.add(user)
    await db_session.commit()
    await grant(db_session, user, world.home, ["manage_project", "view_issues", "view_wiki"])
    return user


async def _login(client: AsyncClient, user: User) -> AsyncClient:
    resp = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
    return client


@pytest.fixture
def queued(monkeypatch) -> list[dict]:
    """Record rebuild tasks instead of sending them to the broker."""
    calls: list[dict] = []

    def fake_apply_async(args=None, task_id=None, **_kwargs):
        calls.append({"args": args, "task_id": task_id})

    monkeypatch.setattr(rebuild_link_graph_task, "apply_async", fake_apply_async)
    monkeypatch.setattr("specivo.api.v1.link_graph_admin._task_state", lambda task_id: ("PENDING", None))
    return calls


class TestDispatch:
    async def test_manager_starts_one_run_and_a_second_is_refused(self, client, db_session, world, manager, queued):
        await _login(client, manager)
        url = f"/api/v1/projects/{world.home.key}/links/rebuild/"
        resp = await client.post(url)
        assert resp.status_code == 202, resp.text
        task_id = resp.json()["task_id"]
        assert queued == [{"args": [world.home.id, manager.login], "task_id": task_id}]
        assert await TokenLock(await get_redis(), rebuild_lock_key(world.home.id)).holder() == task_id

        again = await client.post(url)
        assert again.status_code == 409
        error = again.json()["errors"][0]
        assert error["code"] == "link_rebuild_running"
        assert error["details"]["running"] is True and error["details"]["task_id"] == task_id
        assert len(queued) == 1

        status = await client.get(f"/api/v1/projects/{world.home.key}/links/rebuild/status/")
        assert status.status_code == 200
        assert status.json()["running"] is True

    async def test_a_lock_left_by_a_finished_task_is_cleared(
        self, client, db_session, world, manager, queued, monkeypatch
    ):
        lock = TokenLock(await get_redis(), rebuild_lock_key(world.home.id))
        assert await lock.acquire("finished-task", 300)
        monkeypatch.setattr("specivo.api.v1.link_graph_admin._task_state", lambda task_id: ("SUCCESS", None))
        await _login(client, manager)
        resp = await client.post(f"/api/v1/projects/{world.home.key}/links/rebuild/")
        assert resp.status_code == 202, resp.text
        assert await lock.holder() == resp.json()["task_id"]

    async def test_without_redis_the_dispatch_fails_closed(
        self, client, db_session, world, manager, queued, monkeypatch
    ):
        async def unavailable(self, token, ttl):
            raise LockUnavailableError("down")

        monkeypatch.setattr(TokenLock, "acquire", unavailable)
        await _login(client, manager)
        resp = await client.post(f"/api/v1/projects/{world.home.key}/links/rebuild/")
        assert resp.status_code == 503
        assert resp.json()["errors"][0]["code"] == "lock_unavailable"
        assert queued == []

    async def test_a_reader_without_manage_project_is_refused(self, client, db_session, world, queued):
        await _login(client, world.reader)
        for method, path in (
            ("post", "rebuild/"),
            ("get", "rebuild/status/"),
            ("get", "stats/"),
        ):
            resp = await getattr(client, method)(f"/api/v1/projects/{world.home.key}/links/{path}")
            assert resp.status_code == 403, (path, resp.text)
        resp = await client.post("/api/v1/admin/links/rebuild/")
        assert resp.status_code == 403
        assert queued == []


class TestTask:
    @pytest.fixture
    def fake_work(self, monkeypatch):
        calls: list[int | None] = []
        stored: list[dict] = []

        async def work(factory, project_id, *, triggered_by=None, progress=None):
            calls.append(project_id)
            if progress is not None:
                await progress(1, 1)
            return {"status": "success"}

        async def store(session, project_id, summary):
            stored.append(summary)

        monkeypatch.setattr("specivo.services.link_graph_admin_service.run_full_rebuild", work)
        monkeypatch.setattr("specivo.services.link_graph_admin_service.store_last_result", store)
        return calls, stored

    async def test_releases_its_lock_on_success(self, fake_work):
        calls, _stored = fake_work
        lock = TokenLock(await get_redis(), rebuild_lock_key(987001))
        assert await lock.acquire("task-ok", 300)
        progress: list[tuple[int, int]] = []

        async def report(processed, total):
            progress.append((processed, total))

        assert await _full_rebuild_async("task-ok", 987001, "elr_manager", report) == {"status": "success"}
        assert calls == [987001] and progress == [(1, 1)]
        assert await lock.holder() is None

    async def test_releases_its_lock_and_records_a_failure(self, monkeypatch, fake_work):
        _calls, stored = fake_work

        async def broken(factory, project_id, *, triggered_by=None, progress=None):
            raise RuntimeError("parser exploded")

        monkeypatch.setattr("specivo.services.link_graph_admin_service.run_full_rebuild", broken)
        lock = TokenLock(await get_redis(), rebuild_lock_key(987002))
        assert await lock.acquire("task-bad", 300)

        async def report(processed, total):
            return None

        with pytest.raises(RuntimeError):
            await _full_rebuild_async("task-bad", 987002, None, report)
        assert await lock.holder() is None
        assert stored[-1]["status"] == "failed" and "parser exploded" in stored[-1]["error"]

    async def test_a_run_whose_lock_is_taken_over_stops(self, fake_work, monkeypatch):
        _calls, stored = fake_work
        redis = await get_redis()
        lock = TokenLock(redis, rebuild_lock_key(987004))
        assert await lock.acquire("task-run", 300)

        async def work(factory, project_id, *, triggered_by=None, progress=None):
            await redis.set(rebuild_lock_key(987004), "task-intruder")
            await progress(1, 2)
            return {"status": "success"}

        monkeypatch.setattr("specivo.services.link_graph_admin_service.run_full_rebuild", work)

        async def report(processed, total):
            return None

        with pytest.raises(LockLostError):
            await _full_rebuild_async("task-run", 987004, None, report)
        assert stored[-1]["status"] == "failed"
        assert await lock.holder() == "task-intruder"

    async def test_an_expired_lock_is_taken_again(self, fake_work, monkeypatch):
        redis = await get_redis()
        lock = TokenLock(redis, rebuild_lock_key(987005))
        assert await lock.acquire("task-slow", 300)
        seen: list[str | None] = []

        async def work(factory, project_id, *, triggered_by=None, progress=None):
            await redis.delete(rebuild_lock_key(987005))
            await progress(1, 2)
            seen.append(await lock.holder())
            return {"status": "success"}

        monkeypatch.setattr("specivo.services.link_graph_admin_service.run_full_rebuild", work)

        async def report(processed, total):
            return None

        assert await _full_rebuild_async("task-slow", 987005, None, report) == {"status": "success"}
        assert seen == ["task-slow"] and await lock.holder() is None

    async def test_another_run_holding_the_project_makes_it_a_no_op(self, db_session, fake_work):
        calls, _stored = fake_work
        lock = TokenLock(await get_redis(), rebuild_lock_key(987003))
        assert await lock.acquire("task-dup", 300)
        await db_session.execute(select(func.pg_advisory_lock(LINK_REBUILD_LOCK_NAMESPACE, 987003)))
        try:

            async def report(processed, total):
                return None

            assert await _full_rebuild_async("task-dup", 987003, None, report) == {"status": "skipped"}
        finally:
            await db_session.execute(select(func.pg_advisory_unlock(LINK_REBUILD_LOCK_NAMESPACE, 987003)))
        assert calls == []
        # The run that holds the project owns the Redis lock too: it is left alone.
        assert await lock.holder() == "task-dup"


async def _fixture_project(db: AsyncSession, world: World):
    """Known links: 3 wiki links (1 broken), 2 mentions (1 broken), 1 private note mention, 1 relation."""
    guide = await page(db, world, world.home, "Guide", "See [[Setup]] and [[Missing Page]].")
    setup = await page(db, world, world.home, "Setup")
    first = await issue(db, world, world.home, "First", "Read [[Guide]]")
    second = await issue(db, world, world.home, "Second", f"After {first.display_key} and {world.home.key}-999")
    note = await comment(db, world, first, f"Ping {second.display_key}", private=True)
    db.add(IssueRelation(issue_from_id=first.id, issue_to_id=second.id, relation_type="relates"))
    await db.commit()
    return guide, setup, first, second, note


async def test_full_rebuild_produces_the_expected_links(db_session, world):
    sources = await _fixture_project(db_session, world)
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")
    progress: list[tuple[int, int]] = []

    async def report(processed, total):
        progress.append((processed, total))

    summary = await run_full_rebuild(factory, world.home.id, triggered_by="elr_manager", progress=report)
    # Issues: 2, comments with text: 1, live wiki pages: 2 (Guide, Setup).
    assert summary["sources"] == 5 and progress[-1] == (5, 5)
    assert (summary["status"], summary["edges"], summary["unresolved"], summary["failed"]) == ("success", 6, 2, 0)
    assert summary["triggered_by"] == "elr_manager"
    stored = await last_result(db_session, world.home.id)
    assert stored is not None and stored["edges"] == 6
    assert sources


async def test_stats_counts_and_permissions(client, db_session, world, manager):
    sources = await _fixture_project(db_session, world)
    await rebuild(db_session, *sources)

    await _login(client, manager)
    resp = await client.get(f"/api/v1/projects/{world.home.key}/links/stats/")
    assert resp.status_code == 200, resp.text
    stats = resp.json()
    assert stats["total"] == 6 and stats["unresolved"] == 2
    assert stats["by_kind"] == {"wiki_link": 3, "mention": 3}
    assert stats["by_source"] == {"wiki_page": 2, "issue": 3, "journal": 1}
    assert stats["structured"] == {"relation": 1, "parent": 0, "attachment": 0}
    assert {(u["target_type"], u["target_ref"], u["count"]) for u in stats["top_unresolved"]} == {
        ("wiki_page", "missing-page", 1),
        ("issue", f"{world.home.key}-999", 1),
    }
    assert stats["last_update"] is not None

    status = await client.get(f"/api/v1/projects/{world.home.key}/links/rebuild/status/")
    assert status.json()["stats"]["total"] == 6 and status.json()["running"] is False


async def test_unresolved_refs_from_private_notes_are_shown_to_admins_only(client, db_session, world, manager):
    on = await issue(db_session, world, world.home, "Private notes here")
    note = await comment(db_session, world, on, "Internal: [[Secret Codename Page]]", private=True)
    await rebuild(db_session, note)

    await _login(client, manager)
    stats = (await client.get(f"/api/v1/projects/{world.home.key}/links/stats/")).json()
    assert stats["unresolved"] == 1
    assert stats["top_unresolved"] == []

    await _login(client, world.admin)
    stats = (await client.get(f"/api/v1/projects/{world.home.key}/links/stats/")).json()
    assert [u["target_ref"] for u in stats["top_unresolved"]] == ["secret-codename-page"]


async def test_stats_empty_project(client, db_session, world, manager):
    await _login(client, manager)
    stats = (await client.get(f"/api/v1/projects/{world.home.key}/links/stats/")).json()
    assert (stats["total"], stats["unresolved"], stats["top_unresolved"], stats["last_rebuild"]) == (0, 0, [], None)
