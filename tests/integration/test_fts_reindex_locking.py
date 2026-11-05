"""One search reindex at a time: the dispatch guard and the task's advisory locks.

``POST .../search/reindex/`` takes a Redis run lock (``SET NX EX`` holding the
task id) before queueing, so two requests at once queue a single task and the
other gets 409. An instance reindex and a project reindex never run together
(they rewrite the same rows), while two projects may run side by side. An
instance request is never refused because projects are being reindexed (a
project manager must not be able to starve it): it is queued with a pending
marker that refuses new project runs, and its task waits for the running ones.
The task takes advisory locks in its own namespace (instance: key 0 exclusive;
project: key 0 shared plus its id); a project run is a no-op when they are
held. Locks and the marker are released on success, failure and timeout.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.locks import LockLostError, LockUnavailableError, TokenLock
from specivo.core.redis import get_redis
from specivo.models.project import Project
from specivo.models.user import User
from specivo.tasks.search import (
    FTS_INSTANCE_PENDING_KEY,
    FTS_REINDEX_LOCK_NAMESPACE,
    _locked_reindex,
    fts_lock_key,
)
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory

# Serial: the instance-scope advisory lock (key 0) is shared by every worker on the database.
pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


@pytest.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="ftslock_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def projects(db_session: AsyncSession) -> tuple[Project, Project]:
    first = ProjectFactory.build(key="FTSLA", name="Lock A")
    second = ProjectFactory.build(key="FTSLB", name="Lock B")
    db_session.add_all([first, second])
    await db_session.commit()
    return first, second


@pytest.fixture
async def admin_client(client: AsyncClient, admin: User) -> AsyncClient:
    resp = await client.post("/api/v1/auth/login/", json={"login": admin.login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
    return client


@pytest.fixture
def queued(monkeypatch) -> list[tuple[list, str]]:
    """Record queued reindex tasks; every queued task counts as still running."""
    calls: list[tuple[list, str]] = []

    class _FakeTask:
        def apply_async(self, args=None, task_id=None, **_kwargs):
            calls.append((args, task_id))

    monkeypatch.setattr("specivo.api.v1.search_admin.reindex_fts_task", _FakeTask())
    monkeypatch.setattr("specivo.api.v1.search_admin._task_state", lambda task_id: "STARTED")
    return calls


def _project_url(project: Project) -> str:
    return f"/api/v1/projects/{project.key}/search/reindex/"


_INSTANCE_URL = "/api/v1/admin/search/reindex/"


@pytest.fixture
def work(monkeypatch):
    """Stand-ins for the reindex itself and the stored result (written on the task's own connection)."""
    calls: list[int | None] = []
    stored: list[dict] = []

    async def reindex(project_id, progress_cb):
        calls.append(project_id)
        await progress_cb({"issues": 1})
        return {"status": "success"}

    async def store(project_id, summary):
        stored.append(summary)

    monkeypatch.setattr("specivo.tasks.search._reindex_async", reindex)
    monkeypatch.setattr("specivo.tasks.search._store_result", store)
    return calls, stored


class TestDispatch:
    async def test_a_double_dispatch_queues_one_task(self, admin_client, projects, queued):
        project, _ = projects
        first = await admin_client.post(_project_url(project))
        second = await admin_client.post(_project_url(project))
        assert first.status_code == 202, first.text
        assert second.status_code == 409
        assert second.json()["errors"][0]["code"] == "reindex_running"
        assert queued == [([project.id], first.json()["task_id"])]
        holder = await TokenLock(await get_redis(), fts_lock_key(project.id)).holder()
        assert holder == first.json()["task_id"]

    async def test_a_lock_held_by_a_running_task_refuses_and_a_finished_one_is_cleared(
        self, admin_client, projects, queued, monkeypatch
    ):
        project, _ = projects
        lock = TokenLock(await get_redis(), fts_lock_key(project.id))
        assert await lock.acquire("in-flight", 300)
        assert (await admin_client.post(_project_url(project))).status_code == 409
        assert queued == []

        monkeypatch.setattr("specivo.api.v1.search_admin._task_state", lambda task_id: "SUCCESS")
        resp = await admin_client.post(_project_url(project))
        assert resp.status_code == 202
        assert await lock.holder() == resp.json()["task_id"]

    async def test_project_runs_cannot_starve_an_instance_reindex(self, admin_client, projects, queued):
        first, second = projects
        assert (await admin_client.post(_project_url(first))).status_code == 202
        # Another project may run alongside...
        assert (await admin_client.post(_project_url(second))).status_code == 202
        # ...and the instance request is queued to wait for them, not refused.
        instance = await admin_client.post(_INSTANCE_URL)
        assert instance.status_code == 202, instance.text
        assert instance.json()["state"] == "PENDING_INSTANCE"
        redis = await get_redis()
        assert await TokenLock(redis, FTS_INSTANCE_PENDING_KEY).holder() == instance.json()["task_id"]
        assert len(queued) == 3
        # From now on no project run may start, so the ones going can only drain.
        await TokenLock(redis, fts_lock_key(first.id)).release(queued[0][1])
        again = await admin_client.post(_project_url(first))
        assert again.status_code == 409
        assert "instance-wide" in again.json()["errors"][0]["message"]
        assert len(queued) == 3

    async def test_the_pending_marker_alone_refuses_project_runs(self, admin_client, projects, queued):
        redis = await get_redis()
        assert await TokenLock(redis, FTS_INSTANCE_PENDING_KEY).acquire("instance-waiting", 300)
        resp = await admin_client.post(_project_url(projects[0]))
        assert resp.status_code == 409
        assert await TokenLock(redis, fts_lock_key(projects[0].id)).holder() is None
        assert queued == []

    async def test_an_instance_request_without_project_runs_starts_at_once(self, admin_client, queued):
        resp = await admin_client.post(_INSTANCE_URL)
        assert resp.status_code == 202 and resp.json()["state"] == "PENDING"
        assert await TokenLock(await get_redis(), FTS_INSTANCE_PENDING_KEY).holder() is None

    async def test_a_running_instance_reindex_refuses_projects(self, admin_client, projects, queued):
        project, _ = projects
        assert (await admin_client.post(_INSTANCE_URL)).status_code == 202
        resp = await admin_client.post(_project_url(project))
        assert resp.status_code == 409
        assert await TokenLock(await get_redis(), fts_lock_key(project.id)).holder() is None
        assert len(queued) == 1

    async def test_without_redis_nothing_is_queued(self, admin_client, projects, queued, monkeypatch):
        async def unavailable(self, token, ttl):
            raise LockUnavailableError("down")

        async def unavailable_unless(self, token, ttl, blockers):
            raise LockUnavailableError("down")

        monkeypatch.setattr(TokenLock, "acquire", unavailable)
        monkeypatch.setattr(TokenLock, "acquire_unless", unavailable_unless)
        resp = await admin_client.post(_project_url(projects[0]))
        assert resp.status_code == 503
        assert resp.json()["errors"][0]["code"] == "lock_unavailable"
        assert queued == []


class TestTask:
    async def test_releases_the_run_lock_on_success(self, work):
        calls, _ = work
        lock = TokenLock(await get_redis(), fts_lock_key(987101))
        assert await lock.acquire("fts-ok", 300)
        reported: list[dict] = []
        assert await _locked_reindex("fts-ok", 987101, reported.append) == {"status": "success"}
        assert calls == [987101] and reported == [{"issues": 1}]
        assert await lock.holder() is None

    async def test_releases_the_run_lock_on_failure(self, work, monkeypatch):
        _, stored = work

        async def broken(project_id, progress_cb):
            raise RuntimeError("tokenizer exploded")

        monkeypatch.setattr("specivo.tasks.search._reindex_async", broken)
        lock = TokenLock(await get_redis(), fts_lock_key(987102))
        assert await lock.acquire("fts-bad", 300)
        with pytest.raises(RuntimeError):
            await _locked_reindex("fts-bad", 987102, lambda counts: None)
        assert await lock.holder() is None
        assert stored == [{"status": "failed", "error": "tokenizer exploded"}]

    @pytest.mark.parametrize(
        ("held", "shared", "project_id"),
        [
            (987103, False, 987103),  # the same project (a redelivered message)
            (0, False, 987104),  # an instance reindex
        ],
    )
    async def test_is_a_no_op_while_the_scope_is_held(self, db_session, work, held, shared, project_id):
        calls, _ = work
        lock = TokenLock(await get_redis(), fts_lock_key(project_id))
        assert await lock.acquire("fts-dup", 300)
        take = func.pg_advisory_lock_shared if shared else func.pg_advisory_lock
        release = func.pg_advisory_unlock_shared if shared else func.pg_advisory_unlock
        await db_session.execute(select(take(FTS_REINDEX_LOCK_NAMESPACE, held)))
        try:
            assert await _locked_reindex("fts-dup", project_id, lambda counts: None) == {"status": "skipped"}
        finally:
            await db_session.execute(select(release(FTS_REINDEX_LOCK_NAMESPACE, held)))
        assert calls == []
        assert await lock.holder() == "fts-dup"

    async def test_two_projects_run_side_by_side(self, db_session, work):
        calls, _ = work
        await db_session.execute(select(func.pg_advisory_lock_shared(FTS_REINDEX_LOCK_NAMESPACE, 0)))
        await db_session.execute(select(func.pg_advisory_lock(FTS_REINDEX_LOCK_NAMESPACE, 987105)))
        try:
            assert await _locked_reindex("fts-other", 987106, lambda counts: None) == {"status": "success"}
        finally:
            await db_session.execute(select(func.pg_advisory_unlock(FTS_REINDEX_LOCK_NAMESPACE, 987105)))
            await db_session.execute(select(func.pg_advisory_unlock_shared(FTS_REINDEX_LOCK_NAMESPACE, 0)))
        assert calls == [987106]


class TestInstanceWait:
    """An instance run waits for project runs, then clears the pending marker whatever happens."""

    @pytest.fixture
    def fast(self, monkeypatch):
        monkeypatch.setattr("specivo.tasks.search.FTS_INSTANCE_WAIT_FIRST_DELAY", 0.05)
        monkeypatch.setattr("specivo.tasks.search.FTS_INSTANCE_WAIT_MAX_DELAY", 0.1)

    @pytest.fixture
    async def held_instance(self):
        redis = await get_redis()
        lock = TokenLock(redis, fts_lock_key(None))
        marker = TokenLock(redis, FTS_INSTANCE_PENDING_KEY)
        assert await lock.acquire("fts-instance", 300)
        assert await marker.acquire("fts-instance", 300)
        return lock, marker

    async def test_proceeds_once_project_runs_have_finished(self, db_session, work, fast, held_instance):
        calls, _ = work
        lock, marker = held_instance
        await db_session.execute(select(func.pg_advisory_lock_shared(FTS_REINDEX_LOCK_NAMESPACE, 0)))
        waits: list[int] = []
        run = asyncio.create_task(_locked_reindex("fts-instance", None, lambda counts: None, lambda: waits.append(1)))
        await asyncio.sleep(0.3)
        assert not run.done() and calls == [] and waits
        assert await marker.holder() == "fts-instance"
        await db_session.execute(select(func.pg_advisory_unlock_shared(FTS_REINDEX_LOCK_NAMESPACE, 0)))
        assert await asyncio.wait_for(run, 10) == {"status": "success"}
        assert calls == [None]
        assert await marker.holder() is None and await lock.holder() is None

    async def test_gives_up_after_the_timeout_and_clears_everything(
        self, db_session, work, fast, held_instance, monkeypatch
    ):
        calls, stored = work
        lock, marker = held_instance
        monkeypatch.setattr("specivo.tasks.search.FTS_INSTANCE_WAIT_TIMEOUT", 0.2)
        await db_session.execute(select(func.pg_advisory_lock_shared(FTS_REINDEX_LOCK_NAMESPACE, 0)))
        try:
            result = await _locked_reindex("fts-instance", None, lambda counts: None)
        finally:
            await db_session.execute(select(func.pg_advisory_unlock_shared(FTS_REINDEX_LOCK_NAMESPACE, 0)))
        assert result["status"] == "failed" and stored == [result]
        assert calls == []
        assert await marker.holder() is None and await lock.holder() is None

    async def test_clears_the_marker_on_success_and_on_failure(self, work, fast, held_instance, monkeypatch):
        lock, marker = held_instance
        assert await _locked_reindex("fts-instance", None, lambda counts: None) == {"status": "success"}
        assert await marker.holder() is None and await lock.holder() is None

        async def broken(project_id, progress_cb):
            raise RuntimeError("tokenizer exploded")

        monkeypatch.setattr("specivo.tasks.search._reindex_async", broken)
        assert await lock.acquire("fts-instance-2", 300)
        assert await marker.acquire("fts-instance-2", 300)
        with pytest.raises(RuntimeError):
            await _locked_reindex("fts-instance-2", None, lambda counts: None)
        assert await marker.holder() is None and await lock.holder() is None


class TestInstanceWaitKeepsItsLocks:
    """The waiting instance run checks on every pass that it still holds its lock and the marker."""

    @pytest.fixture
    def fast(self, monkeypatch):
        monkeypatch.setattr("specivo.tasks.search.FTS_INSTANCE_WAIT_FIRST_DELAY", 0.05)
        monkeypatch.setattr("specivo.tasks.search.FTS_INSTANCE_WAIT_MAX_DELAY", 0.05)

    @pytest.fixture
    async def projects_running(self, db_session):
        await db_session.execute(select(func.pg_advisory_lock_shared(FTS_REINDEX_LOCK_NAMESPACE, 0)))
        yield
        await db_session.execute(select(func.pg_advisory_unlock_shared(FTS_REINDEX_LOCK_NAMESPACE, 0)))

    async def test_an_expired_marker_is_taken_again_and_still_refuses_projects(
        self, db_session, work, fast, projects, admin_client, queued
    ):
        calls, _ = work
        redis = await get_redis()
        lock, marker = TokenLock(redis, fts_lock_key(None)), TokenLock(redis, FTS_INSTANCE_PENDING_KEY)
        assert await lock.acquire("fts-wait", 300) and await marker.acquire("fts-wait", 300)
        await db_session.execute(select(func.pg_advisory_lock_shared(FTS_REINDEX_LOCK_NAMESPACE, 0)))
        run = asyncio.create_task(_locked_reindex("fts-wait", None, lambda counts: None))
        try:
            await asyncio.sleep(0.15)
            await redis.delete(FTS_INSTANCE_PENDING_KEY, fts_lock_key(None))  # both expire mid-wait
            await asyncio.sleep(0.2)
            assert await marker.holder() == "fts-wait" and await lock.holder() == "fts-wait"
            resp = await admin_client.post(_project_url(projects[0]))
            assert resp.status_code == 409 and queued == []
        finally:
            await db_session.execute(select(func.pg_advisory_unlock_shared(FTS_REINDEX_LOCK_NAMESPACE, 0)))
        assert await asyncio.wait_for(run, 10) == {"status": "success"}
        assert calls == [None]
        assert await marker.holder() is None and await lock.holder() is None

    async def test_a_marker_taken_by_another_run_stops_the_wait(self, work, fast, projects_running):
        calls, stored = work
        redis = await get_redis()
        lock, marker = TokenLock(redis, fts_lock_key(None)), TokenLock(redis, FTS_INSTANCE_PENDING_KEY)
        assert await lock.acquire("fts-mine", 300)
        assert await marker.acquire("fts-theirs", 300)
        result = await asyncio.wait_for(_locked_reindex("fts-mine", None, lambda counts: None), 10)
        assert result["status"] == "failed" and "took over" in result["error"] and stored == [result]
        assert calls == []
        # Ours is released; theirs is left alone.
        assert await lock.holder() is None and await marker.holder() == "fts-theirs"

    async def test_a_redelivered_task_sets_the_marker_itself(self, db_session, work, fast):
        """A message delivered again finds no marker (it was cleared, or expired) and sets it while it waits."""
        calls, _ = work
        redis = await get_redis()
        marker = TokenLock(redis, FTS_INSTANCE_PENDING_KEY)
        assert await TokenLock(redis, fts_lock_key(None)).acquire("fts-again", 300)
        await db_session.execute(select(func.pg_advisory_lock_shared(FTS_REINDEX_LOCK_NAMESPACE, 0)))
        run = asyncio.create_task(_locked_reindex("fts-again", None, lambda counts: None))
        try:
            await asyncio.sleep(0.2)
            assert await marker.holder() == "fts-again"
        finally:
            await db_session.execute(select(func.pg_advisory_unlock_shared(FTS_REINDEX_LOCK_NAMESPACE, 0)))
        assert await asyncio.wait_for(run, 10) == {"status": "success"}
        assert calls == [None] and await marker.holder() is None

    async def test_a_run_whose_lock_is_taken_over_stops(self, work, monkeypatch):
        _, stored = work
        redis = await get_redis()
        lock = TokenLock(redis, fts_lock_key(987107))
        assert await lock.acquire("fts-run", 300)

        async def reindex(project_id, progress_cb):
            await redis.set(fts_lock_key(987107), "fts-intruder")
            await progress_cb({"issues": 1})
            return {"status": "success"}

        monkeypatch.setattr("specivo.tasks.search._reindex_async", reindex)
        with pytest.raises(LockLostError):
            await _locked_reindex("fts-run", 987107, lambda counts: None)
        assert stored[-1]["status"] == "failed"
        assert await lock.holder() == "fts-intruder"
