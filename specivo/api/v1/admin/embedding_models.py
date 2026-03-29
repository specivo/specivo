"""Admin API for embedding model management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError, PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.search import EmbeddingModel
from specivo.models.user import User

router = APIRouter(tags=["admin"])


def _require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Dependency: raise 403 if the current user is not an admin."""
    if not current_user.is_admin:
        raise PermissionDeniedError("Admin access required")
    return current_user


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


@router.get("/admin/embedding-models", response_model=list[EmbeddingModelOut])
async def list_embedding_models(
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[EmbeddingModel]:
    """List all registered embedding models (admin only)."""
    result = await db.execute(select(EmbeddingModel).order_by(EmbeddingModel.id))
    return list(result.scalars().all())


@router.post("/admin/embedding-models", response_model=EmbeddingModelOut, status_code=201)
async def create_embedding_model(
    payload: EmbeddingModelCreate,
    current_user: User = Depends(_require_admin),
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


@router.delete("/admin/embedding-models/{model_id}", status_code=204)
async def delete_embedding_model(
    model_id: int,
    current_user: User = Depends(_require_admin),
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


@router.post("/admin/embedding-models/{model_id}/backfill", status_code=202)
async def backfill_model(
    model_id: int,
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Trigger backfill of embeddings for all existing chunks using this model (admin only).

    Dispatches a Celery task for async processing.
    """
    result = await db.execute(select(EmbeddingModel).where(EmbeddingModel.id == model_id))
    model = result.scalar_one_or_none()
    if model is None:
        raise NotFoundError(f"Embedding model {model_id} not found")

    from specivo.tasks.embeddings import backfill_model_embeddings

    backfill_model_embeddings.delay(model_id)

    return {"status": "backfill_started", "model_id": model_id}
