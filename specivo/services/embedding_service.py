"""Embedding generation and management service.

Handles:
- Generating embeddings with the bundled local model (or mock for tests)
- Embedding the chunks that have none yet (the Celery task, the import, the backfill)
- Backfilling embeddings for all existing chunks, or re-embedding them all

Chunks themselves are written by :mod:`specivo.services.search_index_service`.
"""

from __future__ import annotations

import hashlib
import logging
import math

from sqlalchemy import ColumnElement, exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.search import ChunkEmbedding, EmbeddingModel, SearchChunk, SearchSource
from specivo.schemas.search import SearchSourceType
from specivo.services.local_embedder import get_local_embedder
from specivo.services.prefix_registry import get_effective_prefix

logger = logging.getLogger(__name__)


async def resolve_attachment_project_id(
    session: AsyncSession,
    container_type: str,
    container_id: int,
) -> int | None:
    """Resolve the project_id for an attachment's container.

    Args:
        session: Async DB session.
        container_type: ``"Issue"``, ``"WikiPage"``, or ``"Journal"``.
        container_id: The container entity's primary key.

    Returns:
        The project ID, or None if the container is not found.
    """
    if container_type == "Issue":
        from specivo.models.issue import Issue

        result = await session.execute(select(Issue.project_id).where(Issue.id == container_id))
        return result.scalar_one_or_none()

    if container_type == "WikiPage":
        from specivo.models.wiki import Wiki, WikiPage

        result = await session.execute(
            select(Wiki.project_id).join(WikiPage, WikiPage.wiki_id == Wiki.id).where(WikiPage.id == container_id)
        )
        return result.scalar_one_or_none()

    if container_type == "Journal":
        from specivo.models.issue import Issue
        from specivo.models.journal import Journal

        result = await session.execute(
            select(Issue.project_id).join(Journal, Journal.issue_id == Issue.id).where(Journal.id == container_id)
        )
        return result.scalar_one_or_none()

    return None


# Only the bundled local model is supported ("mock" is a deterministic stand-in
# for the tests). A row naming any other provider, such as a hosted one left by
# an older release, is treated like a local model whose files are missing: no
# embeddings, and search falls back to keyword matching.
#
# (model id, provider) pairs already warned about in this process.
_warned_unsupported: set[tuple[int | None, str]] = set()


def is_provider_usable(provider: str, model_name: str) -> bool:
    """Whether a model with this provider can embed in this process right now.

    A local model needs its ONNX and tokenizer files; the mock provider (tests)
    always works; any other provider is not supported.
    """
    if provider == "local":
        return get_local_embedder(model_name).is_available()
    return provider == "mock"


def warn_unsupported_provider(model: EmbeddingModel) -> None:
    """Log, once per process and model, that *model* names a provider that cannot run."""
    key = (model.id, model.provider)
    if key in _warned_unsupported:
        return
    _warned_unsupported.add(key)
    logger.warning(
        "Embedding model %s uses provider %r, which is not supported; semantic search is off "
        "and search falls back to keyword matching. Only the bundled local model is supported",
        model.name,
        model.provider,
    )


class EmbeddingService:
    """Manages embedding generation and storage."""

    def is_model_usable(self, model: EmbeddingModel) -> bool:
        """Whether *model* can embed in this process (see :func:`is_provider_usable`)."""
        return is_provider_usable(model.provider, model.model_name)

    async def generate_embedding(
        self,
        text: str,
        model: EmbeddingModel,
        *,
        intent: str = "passage",
    ) -> list[float] | None:
        """Generate an embedding vector for text using the specified model.

        Applies the appropriate prefix based on model config and intent before
        generating the embedding. See prefix_registry.py for prefix conventions.

        Args:
            text: Input text to embed.
            model: The EmbeddingModel configuration.
            intent: "passage" for content being indexed, "query" for search queries.

        Returns:
            The embedding vector, or None when the model cannot run here (its
            files are missing or its provider is not supported).
        """
        prefixed_text = self._apply_prefix(text, model, intent)

        if model.provider == "local":
            embedder = get_local_embedder(model.model_name)
            if not embedder.is_available():
                logger.warning(
                    "Local embedding model %r is not available (missing ONNX/tokenizer files)",
                    model.model_name,
                )
                return None  # type: ignore[return-value]
            return embedder.encode(prefixed_text)

        if model.provider == "mock":
            return self._mock_embedding(prefixed_text, model.dimensions)

        warn_unsupported_provider(model)
        return None

    def _apply_prefix(self, text: str, model: EmbeddingModel, intent: str) -> str:
        """Prepend the appropriate prefix based on model config and intent.

        Uses DB-stored prefix if set (including empty string for "no prefix").
        Falls back to auto-detection from model_name via the prefix registry.
        """
        passage_prefix, query_prefix = get_effective_prefix(
            model.model_name,
            getattr(model, "passage_prefix", None),
            getattr(model, "query_prefix", None),
        )

        prefix = query_prefix if intent == "query" else passage_prefix
        return f"{prefix}{text}" if prefix else text

    def _mock_embedding(self, text: str, dimensions: int) -> list[float]:
        """Generate a deterministic mock embedding from text hash.

        Uses SHA-256 hash of the text to seed a deterministic vector.
        This ensures the same text always produces the same embedding,
        and different texts produce different (but not semantically meaningful) embeddings.
        """
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        # Generate enough float values from the hash
        values: list[float] = []
        for i in range(dimensions):
            # Use different parts of the hash to generate each dimension
            seed_str = f"{digest}:{i}"
            h = hashlib.md5(seed_str.encode("utf-8")).hexdigest()  # noqa: S324
            # Convert hex to a float in [-1, 1]
            int_val = int(h[:8], 16)
            values.append((int_val / 0xFFFFFFFF) * 2 - 1)

        # Normalize to unit vector
        magnitude = math.sqrt(sum(v * v for v in values))
        if magnitude > 0:
            values = [v / magnitude for v in values]

        return values

    async def get_default_model(self, session: AsyncSession) -> EmbeddingModel | None:
        """Get the default embedding model."""
        result = await session.execute(select(EmbeddingModel).where(EmbeddingModel.is_default.is_(True)).limit(1))
        return result.scalar_one_or_none()

    async def _should_skip_comment(
        self,
        session: AsyncSession,
        entity_id: int,
    ) -> bool:
        """Check whether a comment should be skipped based on settings.

        Checks ``search_index_comments`` (global kill switch) and
        ``search_exclude_bot_comments`` (excludes service-account / API-key comments).

        Args:
            session: DB session for looking up the journal.
            entity_id: The journal ID.

        Returns:
            True if the comment should NOT be indexed.
        """
        settings = get_settings()

        if not settings.search_index_comments:
            return True

        if settings.search_exclude_bot_comments:
            from specivo.models.journal import Journal
            from specivo.models.user import User

            result = await session.execute(
                select(Journal.api_key_id, User.is_service_account)
                .join(User, User.id == Journal.user_id)
                .where(Journal.id == entity_id)
            )
            row = result.one_or_none()
            if row is not None:
                api_key_id, is_service_account = row
                if api_key_id is not None or is_service_account:
                    return True

        return False

    async def embed_source(
        self,
        session: AsyncSession,
        source_type: SearchSourceType | str,
        entity_id: int,
        project_id: int,
        chunks: list[str],
        model_id: int | None = None,
    ) -> SearchSource | None:
        """Write a source's chunks, then embed the ones without an embedding, inline.

        Chunks are written whether or not a model can run, so the content is
        keyword-searchable either way. Unchanged chunks keep their embeddings.
        Used by the backfill command; the request path never embeds.

        Args:
            session: Async DB session.
            source_type: One of :class:`SearchSourceType` (``ISSUE``,
                ``WIKI_PAGE``, ``JOURNAL``, ``ATTACHMENT``).
            entity_id: ID of the source entity.
            project_id: Project ID for scoping.
            chunks: List of text chunks.
            model_id: Specific model ID, or None to use the default.

        Returns:
            The SearchSource, or None if there are no chunks or indexing is skipped.
        """
        from specivo.services.search_index_service import write_source_chunks

        if not chunks:
            return None

        # Journal (comment) gating: check settings before indexing
        if source_type == SearchSourceType.JOURNAL:
            if await self._should_skip_comment(session, entity_id):
                return None

        source_type = str(source_type)
        await session.run_sync(
            lambda s: write_source_chunks(s.connection(), source_type, entity_id, project_id, chunks)
        )
        await self.embed_pending(session, [(source_type, entity_id)], model_id=model_id)

        result = await session.execute(
            select(SearchSource).where(SearchSource.source_type == source_type, SearchSource.entity_id == entity_id)
        )
        return result.scalar_one_or_none()

    async def index_source(self, session: AsyncSession, source_type: SearchSourceType | str, entity_id: int) -> int:
        """Rebuild a source's chunks from the database and embed them inline when a model can run.

        Applies the comment settings. Used by bulk callers that run without a
        worker, such as the import pipeline. Returns the number of embeddings written.
        """
        from specivo.services.search_index_service import sync_source_isolated

        source_type = str(source_type)
        await session.run_sync(lambda s: sync_source_isolated(s.connection(), source_type, entity_id))
        return await self.embed_pending(session, [(source_type, entity_id)])

    async def embed_pending(
        self,
        session: AsyncSession,
        sources: list[tuple[str, int]],
        model_id: int | None = None,
        *,
        reembed: bool = False,
    ) -> int:
        """Embed the chunks of *sources* that have no embedding for the model yet.

        Idempotent: chunks already embedded, and sources that no longer exist,
        are skipped. With *reembed*, every chunk of the sources is embedded
        again and its stored embedding replaced. Does nothing when the model
        cannot run here. Returns the number of embeddings written.
        """
        if model_id is not None:
            model = (
                await session.execute(select(EmbeddingModel).where(EmbeddingModel.id == model_id))
            ).scalar_one_or_none()
        else:
            model = await self.get_default_model(session)
        if model is None or not self.is_model_usable(model) or not sources:
            return 0

        count = 0
        for source_type, entity_id in sources:
            stmt = (
                select(SearchChunk.id, SearchChunk.content)
                .join(SearchSource, SearchSource.id == SearchChunk.source_id)
                .where(SearchSource.source_type == source_type, SearchSource.entity_id == entity_id)
                .order_by(SearchChunk.chunk_index)
            )
            if not reembed:
                stmt = stmt.where(_lacks_embedding(model.id))
            for chunk_id, content in (await session.execute(stmt)).all():
                if await self._store_embedding(session, model, chunk_id, content, replace=reembed):
                    count += 1
        return count

    async def _store_embedding(
        self, session: AsyncSession, model: EmbeddingModel, chunk_id: int, content: str, *, replace: bool
    ) -> bool:
        """Embed one chunk and store the vector; True when a vector was written."""
        vector = await self.generate_embedding(content, model)
        if vector is None:
            return False
        stmt = pg_insert(ChunkEmbedding).values(chunk_id=chunk_id, model_id=model.id, embedding=vector)
        if replace:
            stmt = stmt.on_conflict_do_update(
                index_elements=["chunk_id", "model_id"], set_={"embedding": stmt.excluded.embedding}
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=["chunk_id", "model_id"])
        await session.execute(stmt)
        return True

    async def backfill_model(
        self, session: AsyncSession, model_id: int, batch_size: int = 1000, *, reembed: bool = False
    ) -> int:
        """Embed every chunk that has no embedding for *model_id*, or all of them with *reembed*.

        Re-embedding replaces each stored vector in place, so semantic search
        keeps working while it runs. Processes chunks in id order and in batches
        to keep memory flat. Does nothing when the model cannot run here.

        Args:
            session: Async DB session.
            model_id: The model to generate embeddings for.
            batch_size: Number of chunks to process per batch.
            reembed: Recompute existing embeddings too.

        Returns:
            Number of embeddings written.
        """
        model_result = await session.execute(select(EmbeddingModel).where(EmbeddingModel.id == model_id))
        model = model_result.scalar_one_or_none()
        if model is None:
            logger.error("Embedding model %d not found for backfill", model_id)
            return 0
        if not self.is_model_usable(model):
            logger.warning(
                "Embedding model %s cannot run here (missing model files or unsupported provider); nothing embedded",
                model.name,
            )
            return 0

        count = 0
        last_id = 0
        while True:
            stmt = select(SearchChunk.id, SearchChunk.content).where(SearchChunk.id > last_id)
            if not reembed:
                stmt = stmt.where(_lacks_embedding(model.id))
            batch = (await session.execute(stmt.order_by(SearchChunk.id).limit(batch_size))).all()
            if not batch:
                break
            for chunk_id, content in batch:
                if await self._store_embedding(session, model, chunk_id, content, replace=reembed):
                    count += 1
                last_id = chunk_id
            await session.flush()

        logger.info(
            "%s %d embeddings for model %s (id=%d)",
            "Re-embedded" if reembed else "Backfilled",
            count,
            model.name,
            model.id,
        )
        return count


def _lacks_embedding(model_id: int) -> ColumnElement[bool]:
    """WHERE clause: the chunk has no embedding for *model_id*."""
    return ~exists(
        select(ChunkEmbedding.id).where(ChunkEmbedding.chunk_id == SearchChunk.id, ChunkEmbedding.model_id == model_id)
    )
