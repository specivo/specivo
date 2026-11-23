"""The link graph backfill marker: ``projects.links_rebuilt_at``.

- a new project is created stamped (nothing to index);
- a full rebuild stamps its project, an instance-wide one every project;
- the backfill (``queue_backfill_rebuilds``) queues a rebuild only for projects
  never stamped, never twice while one is queued, and not while a rebuild
  started from settings runs;
- the beat task skips its tick while another worker holds its lock;
- a queued backfill finding the project already stamped does nothing;
- the stats and the settings card show the time, or "not indexed yet".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from specivo.core.locks import TokenLock
from specivo.core.redis import get_redis
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.link_graph_admin_service import (
    BACKFILL_TRIGGER,
    backfill_marker_key,
    queue_backfill_rebuilds,
    rebuild_lock_key,
    run_full_rebuild,
)
from specivo.tasks.entity_links import (
    _BACKFILL_LOCK_KEY,
    _full_rebuild_async,
    _queue_backfill_async,
    rebuild_link_graph_task,
)
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory
from tests.integration.link_graph_helpers import World, build_world, grant, issue

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_STAMP = datetime(2026, 3, 1, 0, 30, tzinfo=UTC)


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "EBF")


@pytest.fixture
def queued(monkeypatch) -> list[dict]:
    """Record rebuild tasks instead of sending them to the broker; a lock holder reads as still pending."""
    calls: list[dict] = []

    def fake_apply_async(args=None, task_id=None, **_kwargs):
        calls.append({"args": args, "task_id": task_id})

    monkeypatch.setattr(rebuild_link_graph_task, "apply_async", fake_apply_async)
    monkeypatch.setattr("specivo.services.link_graph_admin_service._task_state", lambda task_id: "PENDING")
    return calls


async def _set_stamp(db: AsyncSession, project: Project, value: datetime | None) -> None:
    await db.execute(update(Project).where(Project.id == project.id).values(links_rebuilt_at=value))
    await db.commit()


async def _stamp(db: AsyncSession, project: Project) -> datetime | None:
    return await db.scalar(select(Project.links_rebuilt_at).where(Project.id == project.id))


async def _login(client: AsyncClient, user: User) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _factory(db: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=db.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")


async def _forget(*project_ids: int) -> None:
    redis = await get_redis()
    for project_id in project_ids:
        await redis.delete(backfill_marker_key(project_id), rebuild_lock_key(project_id))


class TestMarker:
    async def test_a_new_project_is_created_stamped(self, db_session):
        project = ProjectFactory.build(key="EBFNEW", name="Fresh")
        db_session.add(project)
        await db_session.commit()
        assert await _stamp(db_session, project) is not None

    async def test_a_project_created_through_the_api_is_stamped(self, admin_client, db_session):
        resp = await admin_client.post(
            "/api/v1/projects/", json={"name": "Backfill API", "identifier": "ebf-api", "key": "EBFAPI"}
        )
        assert resp.status_code == 201, resp.text
        stamp = await db_session.scalar(select(Project.links_rebuilt_at).where(Project.key == "EBFAPI"))
        assert stamp is not None

    async def test_a_full_rebuild_stamps_its_project_only(self, db_session, world):
        await issue(db_session, world, world.home, "Linked", f"See {world.home.key}-999")
        for project in (world.home, world.hidden):
            await _set_stamp(db_session, project, None)

        summary = await run_full_rebuild(_factory(db_session), world.home.id, triggered_by="test")
        assert summary["status"] == "success"
        assert await _stamp(db_session, world.home) is not None
        assert await _stamp(db_session, world.hidden) is None

    async def test_an_instance_rebuild_stamps_every_project(self, db_session, world):
        for project in (world.home, world.hidden):
            await _set_stamp(db_session, project, None)

        await run_full_rebuild(_factory(db_session), None, triggered_by="test")
        assert await _stamp(db_session, world.home) is not None
        assert await _stamp(db_session, world.hidden) is not None


class TestBackfill:
    async def test_queues_only_projects_never_rebuilt(self, db_session, world, queued):
        await _set_stamp(db_session, world.home, None)
        await _set_stamp(db_session, world.hidden, _STAMP)
        try:
            ids = await queue_backfill_rebuilds(db_session)
            assert world.home.id in ids and world.hidden.id not in ids
            mine = [call for call in queued if call["args"][0] in (world.home.id, world.hidden.id)]
            assert len(mine) == 1
            assert mine[0]["args"] == [world.home.id, BACKFILL_TRIGGER, True]
            # The run lock holds the task id, as a rebuild started from settings does.
            lock = TokenLock(await get_redis(), rebuild_lock_key(world.home.id))
            assert await lock.holder() == mine[0]["task_id"]
        finally:
            await _forget(world.home.id, world.hidden.id)

    async def test_no_second_rebuild_while_one_is_queued(self, db_session, world, queued):
        await _set_stamp(db_session, world.home, None)
        try:
            assert world.home.id in await queue_backfill_rebuilds(db_session)
            assert world.home.id not in await queue_backfill_rebuilds(db_session)
            assert [call["args"][0] for call in queued].count(world.home.id) == 1
        finally:
            await _forget(world.home.id)

    async def test_not_while_a_rebuild_from_settings_runs(self, db_session, world, queued):
        await _set_stamp(db_session, world.home, None)
        lock = TokenLock(await get_redis(), rebuild_lock_key(world.home.id))
        assert await lock.acquire("manual-run", 300)
        try:
            assert world.home.id not in await queue_backfill_rebuilds(db_session)
            assert await lock.holder() == "manual-run"
            # Nothing is marked as queued, so the next tick looks again.
            assert await (await get_redis()).get(backfill_marker_key(world.home.id)) is None
        finally:
            await _forget(world.home.id)

    async def test_a_broker_failure_leaves_nothing_behind(self, db_session, world, monkeypatch):
        def broken(args=None, task_id=None, **_kwargs):
            raise ConnectionError("broker down")

        monkeypatch.setattr(rebuild_link_graph_task, "apply_async", broken)
        await _set_stamp(db_session, world.home, None)
        try:
            assert world.home.id not in await queue_backfill_rebuilds(db_session)
            redis = await get_redis()
            assert await redis.get(backfill_marker_key(world.home.id)) is None
            assert await TokenLock(redis, rebuild_lock_key(world.home.id)).holder() is None
        finally:
            await _forget(world.home.id)

    async def test_the_beat_tick_is_skipped_while_another_worker_holds_it(self, queued):
        lock = TokenLock(await get_redis(), _BACKFILL_LOCK_KEY)
        assert await lock.acquire("other-worker", 60)
        try:
            assert await _queue_backfill_async() == []
            assert queued == []
        finally:
            await lock.release("other-worker")

    async def test_a_backfill_finding_the_project_rebuilt_does_nothing(self, monkeypatch):
        calls: list[int | None] = []

        async def work(factory, project_id, *, triggered_by=None, progress=None):
            calls.append(project_id)
            return {"status": "success"}

        async def rebuilt(session, project_id):
            return True

        monkeypatch.setattr("specivo.services.link_graph_admin_service.run_full_rebuild", work)
        monkeypatch.setattr("specivo.services.link_graph_admin_service.is_rebuilt", rebuilt)
        lock = TokenLock(await get_redis(), rebuild_lock_key(987101))
        assert await lock.acquire("task-backfill", 300)

        async def report(processed, total):
            return None

        result = await _full_rebuild_async("task-backfill", 987101, BACKFILL_TRIGGER, report, backfill=True)
        assert result == {"status": "skipped"}
        assert calls == []
        assert await lock.holder() is None


class TestDisplay:
    @pytest.fixture
    async def manager(self, db_session: AsyncSession, world: World) -> User:
        user = UserFactory.build(login="ebf_manager", status="active", timezone="Asia/Bangkok")
        db_session.add(user)
        await db_session.commit()
        await grant(db_session, user, world.home, ["manage_project", "view_issues", "view_wiki"])
        return user

    async def test_stats_report_the_stamp(self, client, db_session, world, manager):
        headers = await _login(client, manager)
        url = f"/api/v1/projects/{world.home.key}/links/stats/"
        await _set_stamp(db_session, world.home, None)
        assert (await client.get(url, headers=headers)).json()["links_rebuilt_at"] is None
        await _set_stamp(db_session, world.home, _STAMP)
        stamp = (await client.get(url, headers=headers)).json()["links_rebuilt_at"]
        assert datetime.fromisoformat(stamp) == _STAMP

    async def test_the_card_shows_the_last_rebuild_in_the_viewers_timezone(self, client, db_session, world, manager):
        await _set_stamp(db_session, world.home, _STAMP)
        page = await client.get(f"/projects/{world.home.key}/settings/", headers=await _login(client, manager))
        assert page.status_code == 200, page.text
        assert 'data-testid="link-graph-rebuilt-at"' in page.text
        assert "Last rebuilt:" in page.text
        # 00:30 UTC is 07:30 in Bangkok.
        assert "07:30" in page.text
        assert f'datetime="{_STAMP.isoformat()}"' in page.text
        assert "Not indexed yet" not in page.text

    async def test_the_card_says_when_the_project_is_not_indexed_yet(self, client, db_session, world, manager):
        await _set_stamp(db_session, world.home, None)
        page = await client.get(f"/projects/{world.home.key}/settings/", headers=await _login(client, manager))
        assert page.status_code == 200, page.text
        assert "Not indexed yet — rebuild queued" in page.text
        assert "Last rebuilt:" not in page.text
        # The "run a rebuild" hint would contradict the queued rebuild.
        assert 'data-testid="link-graph-empty"' not in page.text
