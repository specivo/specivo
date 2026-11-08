"""AI tools and AI subscriptions API.

- ``GET /ai-tools/`` and ``GET /ai-subscription-presets/``: lookups for every
  signed-in user.
- ``/my/ai-subscriptions/``: the caller's own subscriptions. Reading is always
  allowed; writing needs the instance setting ``ai_subscriptions_self_service``
  (on by default). Account-wide, so a project-limited API key is refused.
- ``/admin/ai-subscriptions/`` and ``/admin/ai-tools/``: administrators, for
  every user. Changes to another user's subscription are audited.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Request, Response

from specivo.api.v1.admin import require_admin_api
from specivo.core.ai_subscriptions import PLAN_PRESETS
from specivo.core.database import DbSession
from specivo.core.exceptions import AppError
from specivo.core.security import get_account_user, get_current_user
from specivo.models.user import User
from specivo.schemas.ai_subscription import (
    AiPlanPresetOut,
    AiSubscriptionAdminCreate,
    AiSubscriptionCreate,
    AiSubscriptionOut,
    AiSubscriptionSettingsOut,
    AiSubscriptionSettingsUpdate,
    AiSubscriptionUpdate,
    AiToolCreate,
    AiToolOut,
    AiToolUpdate,
)
from specivo.schemas.common import PaginatedResponse
from specivo.services.ai_subscription_service import (
    AiSubscriptionService,
    AiToolService,
    SubscriptionInput,
)

router = APIRouter(tags=["ai-subscriptions"])
_service = AiSubscriptionService()
_tools = AiToolService()


def _input(data: AiSubscriptionCreate) -> SubscriptionInput:
    return SubscriptionInput(
        plan_key=data.plan_key,
        provider=data.provider,
        plan_name=data.plan_name,
        tool_slugs=data.tool_slugs,
        amount=data.amount,
        currency=data.currency,
        billing_period=data.billing_period,
        starts_on=data.starts_on,
        ends_on=data.ends_on,
        notes=data.notes,
    )


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


@router.get("/ai-tools/", response_model=list[AiToolOut])
async def list_ai_tools(
    db: DbSession,
    current_user: User = Depends(get_current_user),
    include_inactive: bool = Query(False, description="Also tools not offered in pickers"),
) -> list[AiToolOut]:
    """Tools that report AI usage, in display order. Inactive tools only on request."""
    return [AiToolOut.from_row(t) for t in await _tools.list_tools(db, active_only=not include_inactive)]


@router.get("/ai-subscription-presets/", response_model=list[AiPlanPresetOut])
async def list_presets(current_user: User = Depends(get_current_user)) -> list[AiPlanPresetOut]:
    """Known personal plans with suggested list prices. The prices are hints and may be out of date."""
    return [AiPlanPresetOut.from_preset(p) for p in PLAN_PRESETS]


# ---------------------------------------------------------------------------
# My subscriptions
# ---------------------------------------------------------------------------


@router.get("/my/ai-subscriptions/", response_model=PaginatedResponse[AiSubscriptionOut])
async def list_my_subscriptions(
    db: DbSession,
    current_user: User = Depends(get_account_user),
    active_on: date | None = Query(None, description="Only subscriptions running on this UTC day"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> PaginatedResponse[AiSubscriptionOut]:
    """The caller's own subscriptions, newest start first."""
    rows, total = await _service.list_subscriptions(
        db, user_id=current_user.id, active_on=active_on, offset=offset, limit=limit
    )
    return PaginatedResponse[AiSubscriptionOut](
        total_count=total,
        offset=offset,
        limit=limit,
        items=[AiSubscriptionOut.from_row(r, current_user) for r in rows],
    )


@router.post("/my/ai-subscriptions/", response_model=AiSubscriptionOut, status_code=201)
async def create_my_subscription(
    data: AiSubscriptionCreate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(get_account_user),
) -> AiSubscriptionOut:
    """Add a subscription for the caller.

    403 when self-service is off; 422 ``ai_subscription_owner_invalid`` for a
    service account, ``ai_subscription_invalid``, ``ai_subscription_unknown_tool``,
    ``currency_unknown``, ``currency_inactive``.
    """
    sub = await _service.create(db, _input(data), owner=current_user, actor=current_user, request=request)
    await db.refresh(sub)
    return AiSubscriptionOut.from_row(sub, current_user)


@router.get("/my/ai-subscriptions/{subscription_id}/", response_model=AiSubscriptionOut)
async def get_my_subscription(
    subscription_id: int, db: DbSession, current_user: User = Depends(get_account_user)
) -> AiSubscriptionOut:
    sub = await _service.get_own(db, current_user, subscription_id)
    return AiSubscriptionOut.from_row(sub, current_user)


@router.patch("/my/ai-subscriptions/{subscription_id}/", response_model=AiSubscriptionOut)
async def update_my_subscription(
    subscription_id: int,
    data: AiSubscriptionUpdate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(get_account_user),
) -> AiSubscriptionOut:
    """Change one of the caller's subscriptions (403 when self-service is off)."""
    sub = await _service.get_own(db, current_user, subscription_id)
    sub = await _service.update(db, sub, data.changes(), actor=current_user, request=request)
    await db.refresh(sub)
    return AiSubscriptionOut.from_row(sub, current_user)


@router.delete("/my/ai-subscriptions/{subscription_id}/", status_code=204)
async def delete_my_subscription(
    subscription_id: int, request: Request, db: DbSession, current_user: User = Depends(get_account_user)
) -> Response:
    """Delete one of the caller's subscriptions (403 when self-service is off)."""
    sub = await _service.get_own(db, current_user, subscription_id)
    await _service.delete(db, sub, actor=current_user, request=request)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------


@router.get("/admin/ai-subscriptions/", response_model=PaginatedResponse[AiSubscriptionOut])
async def admin_list_subscriptions(
    db: DbSession,
    current_user: User = Depends(require_admin_api),
    user_id: int | None = Query(None),
    provider: str | None = Query(None, max_length=50),
    active_on: date | None = Query(None, description="Only subscriptions running on this UTC day"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> PaginatedResponse[AiSubscriptionOut]:
    """Everyone's subscriptions, or one user's (admin only)."""
    rows, total = await _service.list_subscriptions(
        db, user_id=user_id, provider=provider, active_on=active_on, offset=offset, limit=limit
    )
    owners = await _service.owners(db, (r.user_id for r in rows))
    return PaginatedResponse[AiSubscriptionOut](
        total_count=total,
        offset=offset,
        limit=limit,
        items=[AiSubscriptionOut.from_row(r, owners.get(r.user_id)) for r in rows],
    )


@router.post("/admin/ai-subscriptions/", response_model=AiSubscriptionOut, status_code=201)
async def admin_create_subscription(
    data: AiSubscriptionAdminCreate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_admin_api),
) -> AiSubscriptionOut:
    """Add a subscription for any person (admin only). Audited when it is not the admin's own."""
    owner = await db.get(User, data.user_id)
    if owner is None:
        raise AppError(code="user_not_found", message="User not found", status_code=422, field="user_id")
    sub = await _service.create(db, _input(data), owner=owner, actor=current_user, request=request)
    await db.refresh(sub)
    return AiSubscriptionOut.from_row(sub, owner)


@router.get("/admin/ai-subscriptions/settings/", response_model=AiSubscriptionSettingsOut)
async def admin_get_settings(
    db: DbSession, current_user: User = Depends(require_admin_api)
) -> AiSubscriptionSettingsOut:
    return AiSubscriptionSettingsOut(self_service=await _service.self_service_enabled(db))


@router.patch("/admin/ai-subscriptions/settings/", response_model=AiSubscriptionSettingsOut)
async def admin_update_settings(
    data: AiSubscriptionSettingsUpdate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_admin_api),
) -> AiSubscriptionSettingsOut:
    """Let people edit their own subscriptions, or not (admin only). Audited when it changes."""
    enabled = await _service.set_self_service(db, data.self_service, actor=current_user, request=request)
    return AiSubscriptionSettingsOut(self_service=enabled)


@router.get("/admin/ai-subscriptions/{subscription_id}/", response_model=AiSubscriptionOut)
async def admin_get_subscription(
    subscription_id: int, db: DbSession, current_user: User = Depends(require_admin_api)
) -> AiSubscriptionOut:
    sub = await _service.get(db, subscription_id)
    return AiSubscriptionOut.from_row(sub, await db.get(User, sub.user_id))


@router.patch("/admin/ai-subscriptions/{subscription_id}/", response_model=AiSubscriptionOut)
async def admin_update_subscription(
    subscription_id: int,
    data: AiSubscriptionUpdate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_admin_api),
) -> AiSubscriptionOut:
    """Change anyone's subscription (admin only). Audited when it is not the admin's own."""
    sub = await _service.get(db, subscription_id)
    sub = await _service.update(db, sub, data.changes(), actor=current_user, request=request)
    await db.refresh(sub)
    return AiSubscriptionOut.from_row(sub, await db.get(User, sub.user_id))


@router.delete("/admin/ai-subscriptions/{subscription_id}/", status_code=204)
async def admin_delete_subscription(
    subscription_id: int, request: Request, db: DbSession, current_user: User = Depends(require_admin_api)
) -> Response:
    """Delete anyone's subscription (admin only). Audited when it is not the admin's own."""
    sub = await _service.get(db, subscription_id)
    await _service.delete(db, sub, actor=current_user, request=request)
    return Response(status_code=204)


@router.get("/admin/ai-tools/", response_model=list[AiToolOut])
async def admin_list_tools(db: DbSession, current_user: User = Depends(require_admin_api)) -> list[AiToolOut]:
    """Every tool, active or not (admin only)."""
    return [AiToolOut.from_row(t) for t in await _tools.list_tools(db, active_only=False)]


@router.post("/admin/ai-tools/", response_model=AiToolOut, status_code=201)
async def admin_create_tool(
    data: AiToolCreate, db: DbSession, current_user: User = Depends(require_admin_api)
) -> AiToolOut:
    """Add a tool (admin only). 409 ``ai_tool_exists``, 422 ``ai_tool_invalid``."""
    tool = await _tools.create(
        db,
        slug=data.slug,
        name=data.name,
        default_provider=data.default_provider,
        is_active=data.is_active,
        position=data.position,
    )
    return AiToolOut.from_row(tool)


@router.patch("/admin/ai-tools/{slug}/", response_model=AiToolOut)
async def admin_update_tool(
    slug: str, data: AiToolUpdate, db: DbSession, current_user: User = Depends(require_admin_api)
) -> AiToolOut:
    """Rename a tool, set its default provider or position, or (de)activate it (admin only).

    Tools are never deleted; deactivate one to hide it from pickers.
    """
    changes = {k: getattr(data, k) for k in data.model_fields_set}
    tool = await _tools.update(db, slug, changes)
    return AiToolOut.from_row(tool)
