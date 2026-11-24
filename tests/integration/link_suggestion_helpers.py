"""Shared setup for the suggested links tests: a mock default model and hand-made chunk embeddings.

Not a test module. Builds on ``link_graph_helpers`` (two projects, an admin and
a reader). Embeddings are written by hand with 4-dimensional vectors, so a
test chooses exactly how close two items are; 4 dimensions also keep the rows
out of the 384-dimension vector index other tests share.
"""

from __future__ import annotations

import math

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.search import ChunkEmbedding, EmbeddingModel, SearchChunk, SearchSource
from specivo.models.wiki import WikiPage


@pytest.fixture(autouse=True)
def _no_queued_tasks(monkeypatch: pytest.MonkeyPatch) -> list:
    """Keep the embedding and suggestion tasks from being queued; record what would have been."""
    queued: list = []
    monkeypatch.setattr("specivo.services.search_index_service.dispatch_embedding", lambda sources: False)
    monkeypatch.setattr(
        "specivo.services.link_suggestion_service.dispatch_compute",
        lambda **kwargs: queued.append(kwargs) or True,
    )
    return queued


async def default_model(db: AsyncSession, name: str) -> EmbeddingModel:
    """A mock embedding model, made the default (the default is unique, so any other loses it)."""
    for other in (await db.execute(select(EmbeddingModel).where(EmbeddingModel.is_default.is_(True)))).scalars():
        other.is_default = False
    await db.flush()
    model = EmbeddingModel(name=name, provider="mock", model_name=f"{name}-4", dimensions=4, is_default=True)
    db.add(model)
    await db.commit()
    return model


def key(item: Issue | WikiPage) -> tuple[str, int]:
    return ("issue", item.id) if isinstance(item, Issue) else ("wiki_page", item.id)


def unit(*values: float) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


def cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(unit(*a), unit(*b), strict=True))


async def embed(db: AsyncSession, model: EmbeddingModel, item: Issue | WikiPage, *vectors: list[float]) -> None:
    """Give the item's chunks these embeddings, one per chunk, adding chunks when there are more vectors."""
    item_type, item_id = key(item)
    source = (
        await db.execute(
            select(SearchSource).where(SearchSource.source_type == item_type, SearchSource.entity_id == item_id)
        )
    ).scalar_one()
    chunks = list(
        (
            await db.execute(
                select(SearchChunk).where(SearchChunk.source_id == source.id).order_by(SearchChunk.chunk_index)
            )
        ).scalars()
    )
    while len(chunks) < len(vectors):
        chunk = SearchChunk(source_id=source.id, chunk_index=len(chunks), content=f"extra chunk {len(chunks)}")
        db.add(chunk)
        await db.flush()
        chunks.append(chunk)
    for chunk, vector in zip(chunks, vectors, strict=False):
        db.add(ChunkEmbedding(chunk_id=chunk.id, model_id=model.id, embedding=vector))
    await db.commit()


async def add_permission(db: AsyncSession, user, project, permission: str) -> None:
    """Add *permission* to the role ``link_graph_helpers.grant`` gave *user* in *project*."""
    from specivo.models.role import Role
    from specivo.services.permission_service import clear_role_cache

    role = (await db.execute(select(Role).where(Role.name == f"role-{project.key}-{user.login}"))).scalar_one()
    role.permissions = [*role.permissions, permission]
    await db.commit()
    clear_role_cache(db)
