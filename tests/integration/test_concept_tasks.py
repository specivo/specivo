"""The Celery side of concept extraction: a run per project (one at a time), and the nightly sweep that queues
only projects with the extraction on and a provider."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.locks import beat_lock
from specivo.services.ai_provider_service import AiProviderService
from specivo.tasks import celery_app
from specivo.tasks import concepts as tasks
from tests.integration.concept_helpers import FakeModel, _no_queued_extraction, concept, enable, give_provider
from tests.integration.link_graph_helpers import World, build_world, issue

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

assert _no_queued_extraction  # the autouse fixture, imported to be active here


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "CXK")


@pytest.fixture
def model(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> FakeModel:
    """Run the task on the test's session, with a fake model in place of the provider's client."""
    fake = FakeModel(answers={"Ledger": [concept("Ledger", "Ledger")]})

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr(tasks, "task_session", session)
    monkeypatch.setattr(AiProviderService, "client_for", staticmethod(lambda row: fake))
    return fake


async def test_the_task_runs_the_extraction_once_at_a_time(db_session, world, model):
    await enable(db_session, world.home, world.admin)
    await issue(db_session, world, world.home, "Ledger rewrite")

    async with beat_lock(tasks.run_lock_key(world.home.id), 60) as acquired:
        assert acquired
        assert await tasks._extract_async(world.home.id) == {"status": "busy"}
    assert model.sent == []

    result = await tasks._extract_async(world.home.id)
    assert result["status"] == "done" and result["extracted"] == 1 and result["mentions"] == 1


async def test_the_sweep_queues_only_enabled_projects_with_a_provider(db_session, world, model, _no_queued_extraction):
    await give_provider(db_session, world.hidden)  # a provider, but switched off
    assert await tasks._sweep_async() == []

    await enable(db_session, world.home, world.admin)
    assert await tasks._sweep_async() == [world.home.id]
    assert _no_queued_extraction == [world.home.id]


async def test_tasks_are_registered_and_the_sweep_is_scheduled():
    assert "specivo.tasks.concepts.extract_concepts" in celery_app.tasks
    schedule = celery_app.conf.beat_schedule["sweep-concept-extraction"]
    assert schedule["task"] == "specivo.tasks.concepts.sweep_concept_extraction"
