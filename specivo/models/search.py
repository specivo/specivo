"""Search models — pgvector embeddings, chunking, and multi-model support."""

from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from specivo.models.base import Base, TimestampMixin


class EmbeddingModel(Base, TimestampMixin):
    """Registry of embedding models (OpenAI, Cohere, Ollama, mock, etc.)."""

    __tablename__ = "embedding_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # openai, cohere, ollama, mock
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)  # text-embedding-3-small
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)  # 1536, 768, etc.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted API key

    # Prefix prepended to text when generating embeddings for indexing.
    # Empty string means no prefix. NULL means "use provider default" (auto-detect).
    passage_prefix: Mapped[str | None] = mapped_column(String(500), nullable=True, default=None)

    # Prefix prepended to text when generating embeddings for search queries.
    # Empty string means no prefix. NULL means "use provider default" (auto-detect).
    query_prefix: Mapped[str | None] = mapped_column(String(500), nullable=True, default=None)

    def __repr__(self) -> str:
        return f"<EmbeddingModel id={self.id} name={self.name!r} provider={self.provider!r}>"


class ProjectEmbeddingConfig(Base, TimestampMixin):
    """Per-project embedding model selection."""

    __tablename__ = "project_embedding_configs"

    __table_args__ = (UniqueConstraint("project_id", "model_id", name="uq_project_embedding_config"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("embedding_models.id", ondelete="CASCADE"), nullable=False)

    model: Mapped[EmbeddingModel] = relationship("EmbeddingModel", lazy="raise")

    def __repr__(self) -> str:
        return f"<ProjectEmbeddingConfig project_id={self.project_id} model_id={self.model_id}>"


class SearchSource(Base, TimestampMixin):
    """Source entity tracked for search indexing."""

    __tablename__ = "search_sources"

    __table_args__ = (
        UniqueConstraint("source_type", "entity_id", name="uq_search_source"),
        Index("ix_search_sources_type_entity", "source_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)  # issue, wiki_page, journal
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)

    chunks: Mapped[list[SearchChunk]] = relationship(
        "SearchChunk",
        back_populates="source",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<SearchSource id={self.id} type={self.source_type!r} entity={self.entity_id}>"


class SearchChunk(Base, TimestampMixin):
    """A chunk of searchable content (text + optional tsvector)."""

    __tablename__ = "search_chunks"

    __table_args__ = (UniqueConstraint("source_id", "chunk_index", name="uq_search_chunk"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("search_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    search_vector = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', content)", persisted=True),
        nullable=True,
    )
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    source: Mapped[SearchSource] = relationship("SearchSource", back_populates="chunks", lazy="raise")
    embeddings: Mapped[list[ChunkEmbedding]] = relationship(
        "ChunkEmbedding",
        back_populates="chunk",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<SearchChunk id={self.id} source_id={self.source_id} index={self.chunk_index}>"


class ChunkEmbedding(Base, TimestampMixin):
    """Vector embedding for a chunk, from a specific model."""

    __tablename__ = "chunk_embeddings"

    __table_args__ = (UniqueConstraint("chunk_id", "model_id", name="uq_chunk_embedding"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chunk_id: Mapped[int] = mapped_column(
        ForeignKey("search_chunks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_id: Mapped[int] = mapped_column(
        ForeignKey("embedding_models.id", ondelete="CASCADE"), nullable=False, index=True
    )
    embedding = mapped_column(Vector(1536), nullable=False)

    chunk: Mapped[SearchChunk] = relationship("SearchChunk", back_populates="embeddings", lazy="raise")
    model: Mapped[EmbeddingModel] = relationship("EmbeddingModel", lazy="raise")

    def __repr__(self) -> str:
        return f"<ChunkEmbedding id={self.id} chunk_id={self.chunk_id} model_id={self.model_id}>"
