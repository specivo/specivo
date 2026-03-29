"""Embedding generation and management service.

Handles:
- Generating embeddings via provider APIs (or mock for tests)
- Creating/updating SearchSource + SearchChunk + ChunkEmbedding records
- Backfilling embeddings for all existing chunks with a new model
"""

from __future__ import annotations

import hashlib
import logging
import math

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.search import ChunkEmbedding, EmbeddingModel, SearchChunk, SearchSource
from specivo.services.chunking_service import ChunkingService
from specivo.services.prefix_registry import get_effective_prefix

logger = logging.getLogger(__name__)

_chunking = ChunkingService()


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


class EmbeddingService:
    """Manages embedding generation and storage."""

    async def generate_embedding(
        self,
        text: str,
        model: EmbeddingModel,
        *,
        intent: str = "passage",
    ) -> list[float]:
        """Generate an embedding vector for text using the specified model.

        Applies the appropriate prefix based on model config and intent before
        generating the embedding. See prefix_registry.py for prefix conventions.

        Args:
            text: Input text to embed.
            model: The EmbeddingModel configuration.
            intent: "passage" for content being indexed, "query" for search queries.

        Returns:
            A list of floats representing the embedding vector.
        """
        prefixed_text = self._apply_prefix(text, model, intent)

        if model.provider == "mock":
            return self._mock_embedding(prefixed_text, model.dimensions)

        # Future: implement real provider calls (openai, cohere, ollama)
        raise NotImplementedError(f"Provider {model.provider!r} not yet implemented")

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

    async def ensure_hnsw_index(self, session: AsyncSession, model: EmbeddingModel) -> None:
        """Create a partial HNSW index for a specific embedding model.

        Uses ``CREATE INDEX IF NOT EXISTS`` so it is safe to call multiple times.
        The semantic search WHERE clause filters by ``model_id``, allowing
        PostgreSQL to select the partial index automatically.
        """
        from sqlalchemy import text

        index_name = f"idx_hnsw_model_{model.id}"
        dims = model.dimensions
        await session.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS {index_name} "
                f"ON chunk_embeddings USING hnsw ((embedding::vector({dims})) vector_cosine_ops) "
                f"WITH (m = 24, ef_construction = 128) "
                f"WHERE model_id = :model_id"
            ),
            {"model_id": model.id},
        )
        logger.info("Ensured HNSW index %s for model %s (dims=%d)", index_name, model.name, dims)

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
        source_type: str,
        entity_id: int,
        project_id: int,
        chunks: list[str],
        model_id: int | None = None,
    ) -> SearchSource | None:
        """Create/update SearchSource + SearchChunks + ChunkEmbeddings.

        Args:
            session: Async DB session.
            source_type: "issue", "wiki_page", "comment", or "journal".
            entity_id: ID of the source entity.
            project_id: Project ID for scoping.
            chunks: List of text chunks to embed.
            model_id: Specific model ID, or None to use default.

        Returns:
            The SearchSource, or None if no model is available or indexing is skipped.
        """
        if not chunks:
            return None

        # Comment-specific gating: check settings before indexing
        if source_type == "comment":
            if await self._should_skip_comment(session, entity_id):
                return None

        # Resolve model
        if model_id is not None:
            model_result = await session.execute(select(EmbeddingModel).where(EmbeddingModel.id == model_id))
            model = model_result.scalar_one_or_none()
        else:
            model = await self.get_default_model(session)

        if model is None:
            logger.warning("No embedding model available for source_type=%s entity_id=%d", source_type, entity_id)
            return None

        # Upsert SearchSource
        source_result = await session.execute(
            select(SearchSource).where(
                SearchSource.source_type == source_type,
                SearchSource.entity_id == entity_id,
            )
        )
        source = source_result.scalar_one_or_none()

        if source is None:
            source = SearchSource(
                source_type=source_type,
                entity_id=entity_id,
                project_id=project_id,
            )
            session.add(source)
            await session.flush()
        else:
            # Delete old chunks (cascade deletes embeddings too)
            await session.execute(delete(SearchChunk).where(SearchChunk.source_id == source.id))
            await session.flush()

        # Create all chunks in a single batch
        chunk_objects = []
        for idx, chunk_text in enumerate(chunks):
            chunk = SearchChunk(
                source_id=source.id,
                chunk_index=idx,
                content=chunk_text,
                metadata_json={
                    "source_type": source_type,
                    "entity_id": entity_id,
                    "project_id": project_id,
                },
            )
            session.add(chunk)
            chunk_objects.append((chunk, chunk_text))

        # Single flush to get all chunk IDs
        await session.flush()

        # Create all embeddings in a single batch
        for chunk, chunk_text in chunk_objects:
            vector = await self.generate_embedding(chunk_text, model)
            embedding = ChunkEmbedding(
                chunk_id=chunk.id,
                model_id=model.id,
                embedding=vector,
            )
            session.add(embedding)

        await session.flush()
        return source

    async def _resolve_attachment_project_id(
        self,
        session: AsyncSession,
        container_type: str,
        container_id: int,
    ) -> int | None:
        """Resolve the project_id for an attachment's container.

        Delegates to the module-level function for reuse.
        """
        return await resolve_attachment_project_id(session, container_type, container_id)

    async def embed_attachment(
        self,
        session: AsyncSession,
        attachment: object,
    ) -> SearchSource | None:
        """Index an attachment for search (filename + description + metadata content).

        When the attachment has JSONB metadata with ``extracted_text`` or
        ``ai_description``, those are included in the search index:
        - ``ai_description`` is concatenated with the description for chunk 0
        - ``extracted_text`` is split into additional chunks (1..N)

        Args:
            session: Async DB session.
            attachment: An ``Attachment`` model instance.

        Returns:
            The SearchSource, or None if project cannot be resolved or no model.
        """
        # Resolve project from the container
        project_id = await self._resolve_attachment_project_id(
            session,
            attachment.container_type,  # type: ignore[attr-defined]
            attachment.container_id,  # type: ignore[attr-defined]
        )
        if project_id is None:
            logger.warning(
                "Cannot resolve project for attachment %d (%s/%d)",
                attachment.id,  # type: ignore[attr-defined]
                attachment.container_type,  # type: ignore[attr-defined]
                attachment.container_id,  # type: ignore[attr-defined]
            )
            return None

        # Extract metadata content fields if available
        meta: dict | None = getattr(attachment, "metadata", None)
        extracted_text: str | None = None
        ai_description: str | None = None

        if meta:
            extracted_text = meta.get("extracted_text")
            ai_description = meta.get("ai_description")

        # Build description: original + AI description (if any)
        description = attachment.description  # type: ignore[attr-defined]
        if ai_description:
            description = f"{description}\n\n{ai_description}" if description else ai_description

        chunks = _chunking.chunk_attachment(
            attachment.filename,  # type: ignore[attr-defined]
            description,
            extracted_text=extracted_text,
        )

        return await self.embed_source(
            session,
            "attachment",
            attachment.id,  # type: ignore[attr-defined]
            project_id,
            chunks,
        )

    async def backfill_model(self, session: AsyncSession, model_id: int, batch_size: int = 1000) -> int:
        """Generate embeddings for all existing chunks using a new model.

        Processes chunks in batches to avoid loading all into memory at once (OOM).

        Args:
            session: Async DB session.
            model_id: The model to generate embeddings for.
            batch_size: Number of chunks to process per batch.

        Returns:
            Number of embeddings created.
        """
        model_result = await session.execute(select(EmbeddingModel).where(EmbeddingModel.id == model_id))
        model = model_result.scalar_one_or_none()
        if model is None:
            logger.error("Embedding model %d not found for backfill", model_id)
            return 0

        # Batched iteration: process chunks in pages to avoid OOM
        count = 0
        last_id = 0

        while True:
            batch_result = await session.execute(
                select(SearchChunk)
                .where(
                    SearchChunk.id > last_id,
                    ~exists(
                        select(ChunkEmbedding.id).where(
                            ChunkEmbedding.chunk_id == SearchChunk.id,
                            ChunkEmbedding.model_id == model_id,
                        )
                    ),
                )
                .order_by(SearchChunk.id)
                .limit(batch_size)
            )
            batch = batch_result.scalars().all()

            if not batch:
                break

            for chunk in batch:
                vector = await self.generate_embedding(chunk.content, model)
                embedding = ChunkEmbedding(
                    chunk_id=chunk.id,
                    model_id=model.id,
                    embedding=vector,
                )
                session.add(embedding)
                count += 1
                last_id = chunk.id

            await session.flush()

        logger.info("Backfilled %d embeddings for model %s (id=%d)", count, model.name, model.id)
        return count
