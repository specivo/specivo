"""The Celery side of suggested links: vectors follow the embedding task, the computation is queued for
what changed, and the nightly sweep queues every project with embeddings."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.link_suggestion import EntitySuggestion
from specivo.tasks import celery_app
from specivo.tasks import embeddings as embedding_tasks
from specivo.tasks import link_suggestions as tasks
from tests.integration.link_graph_helpers import World, build_world, comment, issue, page
from tests.integration.link_suggestion_helpers import _no_queued_tasks, default_model, embed, key

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

assert _no_queued_tasks  # the autouse fixture, imported to be active here

A = [1.0, 0.0, 0.0, 0.0]


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "SGT")


@pytest.fixture
def in_test_session(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the tasks' ``task_session`` on the test's session (its commits are savepoints)."""

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr(tasks, "task_session", session)


async def test_embedding_task_updates_vectors_and_queues_changed_items(db_session, world, _no_queued_tasks):
    model = await default_model(db_session, "sgt-mock")
    root = await issue(db_session, world, world.home, "Root")
    doc = await page(db_session, world, world.home, "Doc")
    note = await comment(db_session, world, root, "A comment")
    await embed(db_session, model, root, A)
    await embed(db_session, model, doc, A)

    sources = [key(root), key(doc), ("journal", note.id)]
    await embedding_tasks._refresh_document_vectors(db_session, model.id, sources)
    assert _no_queued_tasks == [{"items": {key(root), key(doc)}}]

    _no_queued_tasks.clear()
    await embedding_tasks._refresh_document_vectors(db_session, model.id, sources)
    assert _no_queued_tasks == []  # nothing changed, nothing to recompute


async def test_compute_task_runs_for_a_project_or_items(db_session, world, in_test_session):
    model = await default_model(db_session, "sgt-mock")
    root = await issue(db_session, world, world.home, "Root")
    doc = await page(db_session, world, world.home, "Doc")
    await embed(db_session, model, root, A)
    await embed(db_session, model, doc, A)

    assert await tasks._compute_async(world.home.id, []) == 1
    assert await tasks._compute_async(None, [key(root)]) == 1
    rows = (await db_session.execute(select(EntitySuggestion))).scalars().all()
    assert [(r.source, r.target, r.status) for r in rows] == [(key(root), key(doc), "suggested")]


async def test_sweep_queues_every_project_with_embeddings(db_session, world, in_test_session, _no_queued_tasks):
    assert await tasks._sweep_async() == []  # no default model
    model = await default_model(db_session, "sgt-mock")
    root = await issue(db_session, world, world.home, "Root")
    await issue(db_session, world, world.hidden, "Not embedded")
    await embed(db_session, model, root, A)

    assert await tasks._sweep_async() == [world.home.id]
    assert _no_queued_tasks == [{"project_id": world.home.id}]


async def test_tasks_are_registered_and_the_sweep_is_scheduled():
    assert "specivo.tasks.link_suggestions.compute_similar_links" in celery_app.tasks
    schedule = celery_app.conf.beat_schedule["sweep-similar-links"]
    assert schedule["task"] == "specivo.tasks.link_suggestions.sweep_similar_links"
