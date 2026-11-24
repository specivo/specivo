"""The AI provider a project's background AI features call (concept extraction today).

One row per project at most. Specivo speaks the OpenAI-compatible chat
completions API (``POST {base_url}/chat/completions``), which hosted services
and local servers (Ollama, vLLM, LM Studio, llama.cpp) all offer, so an
air-gapped install points ``base_url`` at a model on its own network.

The API key is optional (local servers rarely need one) and is stored sealed
with ``secret_box`` (purpose ``project-ai-provider``); it is never returned by
the API. Content of a project is only ever sent to the provider of that same
project.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base

#: ``secret_box`` purpose of the stored API keys.
AI_PROVIDER_SECRET_PURPOSE = "project-ai-provider"  # noqa: S105 - a key derivation label, not a secret


class ProjectAiProvider(Base):
    """A project's OpenAI-compatible chat completions endpoint: where, which model, and its key."""

    __tablename__ = "project_ai_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    # Provider name for pricing the usage (``openai``, ``openrouter``, ``ollama``, ...).
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<ProjectAiProvider project={self.project_id} model={self.model!r}>"
