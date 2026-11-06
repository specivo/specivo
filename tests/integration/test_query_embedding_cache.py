"""The short-lived Redis cache of query embeddings.

A repeated or paged query must not run the embedding model again, and the
cache must never change what a query embeds to, or fail a search when Redis
is gone.
"""

from __future__ import annotations

import pytest

from specivo.core.config import get_settings
from specivo.models.search import EmbeddingModel
from specivo.services import query_embedding_cache
from specivo.services.embedding_service import EmbeddingService
from specivo.services.query_embedding_cache import embed_query

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


class _Counting:
    """A deterministic fake embedder that counts its calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def method(self):
        """The fake as a function, so it binds like the method it replaces."""

        async def generate_embedding(service, text_: str, model: EmbeddingModel, intent: str = "passage"):
            self.calls.append(text_)
            return [float(len(text_)), 0.25, -0.5]

        return generate_embedding


def _model(model_id: int = 1, prefix: str | None = None) -> EmbeddingModel:
    return EmbeddingModel(
        id=model_id, name=f"m{model_id}", provider="mock", model_name="mock-3", dimensions=3, query_prefix=prefix
    )


@pytest.fixture(autouse=True)
def _flushed_redis(db_session) -> None:
    """``db_session`` flushes this worker's Redis database at teardown."""


@pytest.fixture
def counting(monkeypatch: pytest.MonkeyPatch) -> _Counting:
    fake = _Counting()
    monkeypatch.setattr(EmbeddingService, "generate_embedding", fake.method())
    monkeypatch.setattr(get_settings(), "search_query_embedding_cache_seconds", 300)
    return fake


async def test_a_repeated_query_runs_the_model_once(counting: _Counting) -> None:
    first = await embed_query(EmbeddingService(), "slow login page", _model())
    second = await embed_query(EmbeddingService(), "slow login page", _model())

    assert first == second == [15.0, 0.25, -0.5]
    assert len(counting.calls) == 1


async def test_whitespace_is_normalised(counting: _Counting) -> None:
    await embed_query(EmbeddingService(), "slow  login\tpage ", _model())
    await embed_query(EmbeddingService(), " slow login page", _model())

    assert counting.calls == ["slow login page"]


async def test_case_is_kept(counting: _Counting) -> None:
    """Some models are cased, so case is part of the query."""
    await embed_query(EmbeddingService(), "Login", _model())
    await embed_query(EmbeddingService(), "login", _model())

    assert len(counting.calls) == 2


async def test_each_model_and_prefix_has_its_own_entry(counting: _Counting) -> None:
    await embed_query(EmbeddingService(), "login", _model(1))
    await embed_query(EmbeddingService(), "login", _model(2))
    await embed_query(EmbeddingService(), "login", _model(1, prefix="query: "))

    assert len(counting.calls) == 3


async def test_zero_seconds_turns_the_cache_off(counting: _Counting, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "search_query_embedding_cache_seconds", 0)

    await embed_query(EmbeddingService(), "login", _model())
    await embed_query(EmbeddingService(), "login", _model())

    assert len(counting.calls) == 2


async def test_a_failed_embedding_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def _unavailable(service, text_: str, model: EmbeddingModel, intent: str = "passage") -> None:
        calls.append(text_)
        return None

    monkeypatch.setattr(EmbeddingService, "generate_embedding", _unavailable)

    assert await embed_query(EmbeddingService(), "login", _model()) is None
    assert await embed_query(EmbeddingService(), "login", _model()) is None
    assert len(calls) == 2


async def test_redis_down_still_embeds(counting: _Counting, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _down():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(query_embedding_cache, "get_redis", _down)

    assert await embed_query(EmbeddingService(), "login", _model()) == [5.0, 0.25, -0.5]
    assert await embed_query(EmbeddingService(), "login", _model()) == [5.0, 0.25, -0.5]
    assert len(counting.calls) == 2


async def test_a_corrupt_entry_is_ignored(counting: _Counting) -> None:
    from specivo.core.redis import get_redis

    redis = await get_redis()
    await redis.set(query_embedding_cache.cache_key("login", _model()), "not base64 floats!")

    assert await embed_query(EmbeddingService(), "login", _model()) == [5.0, 0.25, -0.5]
    assert len(counting.calls) == 1
