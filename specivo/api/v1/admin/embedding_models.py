"""Admin API for embedding model management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import get_db
from specivo.core.exceptions import AppError, NotFoundError
from specivo.models.search import EmbeddingModel
from specivo.models.user import User

router = APIRouter(tags=["admin"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class EmbeddingModelCreate(BaseModel):
    name: str
    provider: str
    model_name: str
    dimensions: int
    is_default: bool = False
    api_key_encrypted: str | None = None
    passage_prefix: str | None = None  # NULL = auto-detect from model name
    query_prefix: str | None = None  # NULL = auto-detect from model name


class EmbeddingModelOut(BaseModel):
    id: int
    name: str
    provider: str
    model_name: str
    dimensions: int
    is_default: bool
    passage_prefix: str | None = None
    query_prefix: str | None = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/admin/embedding-models/", response_model=list[EmbeddingModelOut])
async def list_embedding_models(
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> list[EmbeddingModel]:
    """List all registered embedding models (admin only)."""
    result = await db.execute(select(EmbeddingModel).order_by(EmbeddingModel.id))
    return list(result.scalars().all())


@router.post("/admin/embedding-models/", response_model=EmbeddingModelOut, status_code=201)
async def create_embedding_model(
    payload: EmbeddingModelCreate,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> EmbeddingModel:
    """Register a new embedding model (admin only)."""
    model = EmbeddingModel(
        name=payload.name,
        provider=payload.provider,
        model_name=payload.model_name,
        dimensions=payload.dimensions,
        is_default=payload.is_default,
        api_key_encrypted=payload.api_key_encrypted,
        passage_prefix=payload.passage_prefix,
        query_prefix=payload.query_prefix,
    )
    db.add(model)
    await db.flush()
    await db.refresh(model)
    return model


@router.delete("/admin/embedding-models/{model_id}/", status_code=204)
async def delete_embedding_model(
    model_id: int,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete an embedding model (admin only)."""
    result = await db.execute(select(EmbeddingModel).where(EmbeddingModel.id == model_id))
    model = result.scalar_one_or_none()
    if model is None:
        raise NotFoundError(f"Embedding model {model_id} not found")
    await db.delete(model)
    await db.flush()
    return Response(status_code=204)


@router.post("/admin/embedding-models/{model_id}/backfill/", status_code=202)
async def backfill_model(
    model_id: int,
    reembed: bool = Query(
        False, description="Recompute the embedding of every chunk, not only of chunks that have none."
    ),
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Embed existing chunks with this model in the background (admin only).

    By default only chunks without an embedding for the model are embedded.
    With ``reembed=true`` every chunk is embedded again and its vector replaced
    in place, for example after the model files were replaced. Chunk text is
    kept in step with content as it is saved, so it needs no rebuilding here;
    the ``backfill_embeddings`` command also rebuilds chunks.

    Runs as a task on the embedding queue. Answers 409 when the model cannot
    run, because its files are missing or its provider is not supported.
    """
    from specivo.services.embedding_service import EmbeddingService

    result = await db.execute(select(EmbeddingModel).where(EmbeddingModel.id == model_id))
    model = result.scalar_one_or_none()
    if model is None:
        raise NotFoundError(f"Embedding model {model_id} not found")
    if not EmbeddingService().is_model_usable(model):
        raise AppError(
            code="embedding_model_unavailable",
            message=(
                f"Embedding model {model.name} cannot run: its files are missing from data/models "
                "or its provider is not supported"
            ),
            status_code=409,
        )

    from specivo.tasks.embeddings import backfill_model_embeddings

    backfill_model_embeddings.delay(model_id, reembed)

    return {"status": "backfill_started", "model_id": model_id, "reembed": reembed}
