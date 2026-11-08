"""A short-lived Redis cache of search query embeddings.

Embedding a query runs the model on the server's CPU. People repeat queries,
page through results (each page is a new request) and search for the same
popular terms, so the vector is kept in Redis for
``SEARCH_QUERY_EMBEDDING_CACHE_SECONDS`` (default 300; 0 turns it off).

The key holds everything that decides the vector: the model row, its name,
its size, the query prefix it uses, and the query text with runs of
whitespace collapsed and the ends trimmed. Case is kept, since some models
are cased. The text embedded is that same normalised text, so a cache hit and
a miss give the same vector.

Vectors are stored as base64 float32, the precision pgvector stores them at.
A missing, unreadable or unavailable cache means the query is embedded as if
there were no cache; a failure here never fails a search.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from array import array

from specivo.core.config import get_settings
from specivo.core.redis import get_redis
from specivo.models.search import EmbeddingModel
from specivo.services.embedding_service import EmbeddingService

logger = logging.getLogger(__name__)

_KEY_PREFIX = "qemb:v1"
_WHITESPACE = re.compile(r"\s+")


def normalise_query(text: str) -> str:
    """*text* with runs of whitespace collapsed to one space and the ends trimmed."""
    return _WHITESPACE.sub(" ", text).strip()


def cache_key(text: str, model: EmbeddingModel) -> str:
    """The Redis key for the embedding of *text* by *model*."""
    material = "\x00".join(
        (
            model.model_name,
            str(model.dimensions),
            "" if model.query_prefix is None else f"={model.query_prefix}",
            normalise_query(text),
        )
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}:{model.id}:{digest}"


def _encode(vector: list[float]) -> str:
    return base64.b64encode(array("f", vector).tobytes()).decode("ascii")


def _decode(value: str, dimensions: int) -> list[float] | None:
    try:
        raw = base64.b64decode(value, validate=True)
        floats = array("f")
        floats.frombytes(raw)
    except (ValueError, TypeError):
        return None
    if len(floats) != dimensions:
        return None
    return floats.tolist()


async def embed_query(service: EmbeddingService, text: str, model: EmbeddingModel) -> list[float] | None:
    """The query embedding of *text*, from the cache when it holds one.

    Returns None when the model cannot run here, like
    :meth:`EmbeddingService.generate_embedding`; that answer is not cached.
    Exceptions from the model propagate, so the caller's handling of a broken
    model is unchanged.
    """
    query = normalise_query(text)
    ttl = get_settings().search_query_embedding_cache_seconds
    if ttl <= 0:
        return await service.generate_embedding(query, model, intent="query")

    key = cache_key(query, model)
    redis = None
    try:
        redis = await get_redis()
        cached = await redis.get(key)
    except Exception as exc:  # noqa: BLE001 - the cache is optional
        logger.debug("Query embedding cache unavailable (%s)", exc)
        cached = None
    if cached:
        vector = _decode(cached, model.dimensions)
        if vector is not None:
            return vector

    vector = await service.generate_embedding(query, model, intent="query")
    if vector is not None and redis is not None:
        try:
            await redis.set(key, _encode(vector), ex=ttl)
        except Exception as exc:  # noqa: BLE001 - the cache is optional
            logger.debug("Could not store a query embedding (%s)", exc)
    return vector
