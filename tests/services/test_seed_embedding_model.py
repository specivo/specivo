"""Service tests for seed_embedding_model() against a real database."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.cli.seed import seed_embedding_model
from specivo.models.search import EmbeddingModel

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_NAME = "multilingual-e5-small"


@pytest.fixture(autouse=True)
async def _no_embedding_models(db_session: AsyncSession) -> None:
    """Start from an empty table; the rollback at teardown restores whatever a migration seeded."""
    await db_session.execute(delete(EmbeddingModel))
    await db_session.flush()


async def _all(db: AsyncSession) -> list[EmbeddingModel]:
    return list((await db.execute(select(EmbeddingModel))).scalars().all())


async def test_creates_the_default_local_model(db_session: AsyncSession) -> None:
    await seed_embedding_model(db_session)

    (model,) = await _all(db_session)
    assert model.name == _NAME
    assert model.model_name == _NAME
    assert model.provider == "local"
    assert model.dimensions == 384
    assert model.is_default is True


async def test_is_idempotent(db_session: AsyncSession) -> None:
    await seed_embedding_model(db_session)
    await seed_embedding_model(db_session)

    assert len(await _all(db_session)) == 1


async def test_does_not_overwrite_admin_changes(db_session: AsyncSession) -> None:
    db_session.add(
        EmbeddingModel(name=_NAME, provider="ollama", model_name=_NAME, dimensions=512, is_default=True),
    )
    await db_session.flush()

    await seed_embedding_model(db_session)

    (model,) = await _all(db_session)
    assert (model.provider, model.dimensions) == ("ollama", 512)


async def test_new_model_is_not_default_when_another_row_is(db_session: AsyncSession) -> None:
    db_session.add(EmbeddingModel(name="other", provider="ollama", model_name="other", dimensions=768, is_default=True))
    await db_session.flush()

    await seed_embedding_model(db_session)

    by_name = {m.name: m for m in await _all(db_session)}
    assert by_name[_NAME].is_default is False
    assert by_name["other"].is_default is True


async def test_existing_model_becomes_default_when_none_is(db_session: AsyncSession) -> None:
    db_session.add(EmbeddingModel(name=_NAME, provider="local", model_name=_NAME, dimensions=384, is_default=False))
    await db_session.flush()

    await seed_embedding_model(db_session)

    (model,) = await _all(db_session)
    assert model.is_default is True


async def test_existing_non_default_model_left_alone_when_another_is_default(db_session: AsyncSession) -> None:
    db_session.add(EmbeddingModel(name="other", provider="ollama", model_name="other", dimensions=768, is_default=True))
    db_session.add(EmbeddingModel(name=_NAME, provider="local", model_name=_NAME, dimensions=384, is_default=False))
    await db_session.flush()

    await seed_embedding_model(db_session)

    by_name = {m.name: m for m in await _all(db_session)}
    assert by_name[_NAME].is_default is False
