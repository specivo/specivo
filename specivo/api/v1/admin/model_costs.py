"""Admin model cost config API — CRUD for AI model pricing."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.agent_cost import ModelCostConfigCreate, ModelCostConfigOut
from specivo.services.agent_cost_service import AgentCostService

router = APIRouter(tags=["admin"])
_service = AgentCostService()


def _require_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise PermissionDeniedError("Admin access required")
    return current_user


@router.post(
    "/admin/model-costs",
    response_model=ModelCostConfigOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_model_cost(
    data: ModelCostConfigCreate,
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> ModelCostConfigOut:
    config = await _service.create_model_cost(db, data)
    return ModelCostConfigOut.model_validate(config)


@router.get("/admin/model-costs", response_model=list[ModelCostConfigOut])
async def list_model_costs(
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[ModelCostConfigOut]:
    configs = await _service.list_model_costs(db)
    return [ModelCostConfigOut.model_validate(c) for c in configs]


@router.delete(
    "/admin/model-costs/{config_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_model_cost(
    config_id: int,
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await _service.delete_model_cost(db, config_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
