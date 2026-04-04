"""Admin workflow API — CRUD for transitions and field rules."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import get_db
from specivo.models.user import User
from specivo.schemas.workflow import (
    BulkTransitionReplace,
    FieldRuleCreate,
    FieldRuleOut,
    TransitionCreate,
    TransitionOut,
)
from specivo.services.workflow_service import WorkflowService

router = APIRouter(tags=["admin"])
_service = WorkflowService()


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


@router.get(
    "/admin/workflows/transitions/",
    response_model=list[TransitionOut],
)
async def list_transitions(
    tracker_id: int | None = Query(default=None),
    role_id: int | None = Query(default=None),
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> list[TransitionOut]:
    """List workflow transitions (admin only)."""
    transitions = await _service.list_transitions(db, tracker_id=tracker_id, role_id=role_id)
    return [TransitionOut.model_validate(t) for t in transitions]


@router.post(
    "/admin/workflows/transitions/",
    response_model=TransitionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_transition(
    data: TransitionCreate,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> TransitionOut:
    """Create a workflow transition (admin only)."""
    transition = await _service.create_transition(db, data)
    return TransitionOut.model_validate(transition)


@router.delete(
    "/admin/workflows/transitions/{transition_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_transition(
    transition_id: int,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a workflow transition (admin only)."""
    await _service.delete_transition(db, transition_id)


@router.put(
    "/admin/workflows/transitions/bulk/",
    response_model=list[TransitionOut],
)
async def bulk_replace_transitions(
    data: BulkTransitionReplace,
    tracker_id: int = Query(),
    role_id: int = Query(),
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> list[TransitionOut]:
    """Replace all transitions for tracker+role (admin only)."""
    transitions = await _service.bulk_replace_transitions(db, tracker_id, role_id, data.transitions)
    return [TransitionOut.model_validate(t) for t in transitions]


# ---------------------------------------------------------------------------
# Field Rules
# ---------------------------------------------------------------------------


@router.get(
    "/admin/workflows/field-rules/",
    response_model=list[FieldRuleOut],
)
async def list_field_rules(
    tracker_id: int | None = Query(default=None),
    role_id: int | None = Query(default=None),
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> list[FieldRuleOut]:
    """List workflow field rules (admin only)."""
    rules = await _service.list_field_rules(db, tracker_id=tracker_id, role_id=role_id)
    return [FieldRuleOut.model_validate(r) for r in rules]


@router.post(
    "/admin/workflows/field-rules/",
    response_model=FieldRuleOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_field_rule(
    data: FieldRuleCreate,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> FieldRuleOut:
    """Create a workflow field rule (admin only)."""
    rule = await _service.create_field_rule(db, data)
    return FieldRuleOut.model_validate(rule)


@router.delete(
    "/admin/workflows/field-rules/{rule_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_field_rule(
    rule_id: int,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a workflow field rule (admin only)."""
    await _service.delete_field_rule(db, rule_id)
