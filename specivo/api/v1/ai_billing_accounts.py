"""AI billing accounts API.

- ``/my/ai-billing-accounts/``: the instance (company) accounts and the
  caller's own personal accounts. Reading is always allowed; creating and
  changing personal accounts needs the instance setting
  ``ai_subscriptions_self_service`` (on by default). Instance accounts are
  read-only here. Account-wide, so a project-limited API key is refused.
- ``/admin/ai-billing-accounts/``: administrators, for instance accounts and
  everyone's personal accounts. Changes to instance accounts and to other
  people's accounts are audited.

Accounts are deactivated, never deleted: usage entries keep pointing at them.
Creating an account with route patterns, or changing what decides billing,
resolves the affected usage entries again in the same request.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import DbSession
from specivo.core.exceptions import AppError, NotFoundError
from specivo.core.security import get_account_user
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.user import User
from specivo.schemas.ai_billing_account import (
    AiBillingAccountAdminCreate,
    AiBillingAccountCreate,
    AiBillingAccountOut,
    AiBillingAccountUpdate,
)
from specivo.schemas.common import PaginatedResponse
from specivo.services.ai_billing_account_service import AiBillingAccountService, BillingAccountInput
from specivo.services.ai_subscription_service import AiSubscriptionService

router = APIRouter(tags=["ai-billing-accounts"])
_service = AiBillingAccountService()
_subscriptions = AiSubscriptionService()


def _input(data: AiBillingAccountCreate) -> BillingAccountInput:
    return BillingAccountInput(
        slug=data.slug,
        name=data.name,
        kind=data.kind,
        payer=data.payer,
        reimburse_share=data.reimburse_share,
        route_patterns=data.route_patterns,
        tool_slugs=data.tool_slugs,
        default_cost_type=data.default_cost_type,
        is_active=data.is_active,
    )


async def _out(db: DbSession, rows: list[AiBillingAccount]) -> list[AiBillingAccountOut]:
    owners = await _subscriptions.owners(db, (r.owner_user_id for r in rows if r.owner_user_id is not None))
    links = await _service.subscription_links(db, (r.id for r in rows))
    return [
        AiBillingAccountOut.from_row(
            r, owners.get(r.owner_user_id) if r.owner_user_id is not None else None, links.get(r.id)
        )
        for r in rows
    ]


async def _one(db: DbSession, account: AiBillingAccount) -> AiBillingAccountOut:
    await db.refresh(account)
    [out] = await _out(db, [account])
    return out


async def _own(db: DbSession, user: User, account_id: int) -> AiBillingAccount:
    """*user*'s own account; 404 for an instance account or somebody else's (administrators included)."""
    account = await _service.get(db, account_id)
    if account.owner_user_id != user.id:
        raise NotFoundError("AI billing account not found")
    return account


# ---------------------------------------------------------------------------
# My billing accounts
# ---------------------------------------------------------------------------


@router.get("/my/ai-billing-accounts/", response_model=list[AiBillingAccountOut])
async def list_my_billing_accounts(
    db: DbSession,
    current_user: User = Depends(get_account_user),
    include_inactive: bool = Query(False, description="Also accounts that were switched off"),
) -> list[AiBillingAccountOut]:
    """The instance accounts (which anyone may name in a reporter's configuration), then the caller's own."""
    return await _out(db, await _service.list_visible(db, current_user, active_only=not include_inactive))


@router.post("/my/ai-billing-accounts/", response_model=AiBillingAccountOut, status_code=201)
async def create_my_billing_account(
    data: AiBillingAccountCreate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(get_account_user),
) -> AiBillingAccountOut:
    """Add a personal account for the caller.

    403 when self-service is off; 422 ``ai_billing_account_invalid`` (with the
    field) or ``ai_billing_account_owner_invalid`` for a service account; 409
    ``ai_billing_account_exists`` when the caller has an account with the slug.
    """
    account = await _service.create(db, _input(data), owner=current_user, actor=current_user, request=request)
    return await _one(db, account)


@router.get("/my/ai-billing-accounts/{account_id}/", response_model=AiBillingAccountOut)
async def get_my_billing_account(
    account_id: int, db: DbSession, current_user: User = Depends(get_account_user)
) -> AiBillingAccountOut:
    """An instance account, or one of the caller's own (404 for anybody else's)."""
    account = await _service.get(db, account_id)
    if account.owner_user_id is not None and account.owner_user_id != current_user.id:
        raise NotFoundError("AI billing account not found")
    return await _one(db, account)


@router.patch("/my/ai-billing-accounts/{account_id}/", response_model=AiBillingAccountOut)
async def update_my_billing_account(
    account_id: int,
    data: AiBillingAccountUpdate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(get_account_user),
) -> AiBillingAccountOut:
    """Change one of the caller's own accounts (403 when self-service is off)."""
    account = await _own(db, current_user, account_id)
    account = await _service.update(db, account, data.changes(), actor=current_user, request=request)
    return await _one(db, account)


@router.post("/my/ai-billing-accounts/{account_id}/deactivate/", response_model=AiBillingAccountOut)
async def deactivate_my_billing_account(
    account_id: int, request: Request, db: DbSession, current_user: User = Depends(get_account_user)
) -> AiBillingAccountOut:
    """Switch off one of the caller's own accounts; ``PATCH`` with ``is_active: true`` turns it on again."""
    account = await _own(db, current_user, account_id)
    account = await _service.deactivate(db, account, actor=current_user, request=request)
    return await _one(db, account)


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------


@router.get("/admin/ai-billing-accounts/", response_model=PaginatedResponse[AiBillingAccountOut])
async def admin_list_billing_accounts(
    db: DbSession,
    current_user: User = Depends(require_admin_api),
    scope: Literal["instance", "personal", "all"] = Query(
        "instance", description="instance: company accounts; personal: people's (with user_id, one person's)"
    ),
    user_id: int | None = Query(None, description="With scope=personal: one person's accounts"),
    include_inactive: bool = Query(True),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> PaginatedResponse[AiBillingAccountOut]:
    """Instance accounts, or people's personal accounts (admin only)."""
    rows, total = await _service.list_accounts(
        db,
        scope=scope,
        owner_user_id=user_id,
        include_inactive=include_inactive,
        offset=offset,
        limit=limit,
    )
    return PaginatedResponse[AiBillingAccountOut](
        total_count=total, offset=offset, limit=limit, items=await _out(db, rows)
    )


@router.post("/admin/ai-billing-accounts/", response_model=AiBillingAccountOut, status_code=201)
async def admin_create_billing_account(
    data: AiBillingAccountAdminCreate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_admin_api),
) -> AiBillingAccountOut:
    """Add an instance account, or a personal account for ``owner_user_id`` (admin only). Audited."""
    owner: User | None = None
    if data.owner_user_id is not None:
        owner = await db.get(User, data.owner_user_id)
        if owner is None:
            raise AppError(code="user_not_found", message="User not found", status_code=422, field="owner_user_id")
    account = await _service.create(db, _input(data), owner=owner, actor=current_user, request=request)
    return await _one(db, account)


@router.get("/admin/ai-billing-accounts/{account_id}/", response_model=AiBillingAccountOut)
async def admin_get_billing_account(
    account_id: int, db: DbSession, current_user: User = Depends(require_admin_api)
) -> AiBillingAccountOut:
    return await _one(db, await _service.get(db, account_id))


@router.patch("/admin/ai-billing-accounts/{account_id}/", response_model=AiBillingAccountOut)
async def admin_update_billing_account(
    account_id: int,
    data: AiBillingAccountUpdate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_admin_api),
) -> AiBillingAccountOut:
    """Change any account (admin only). Audited for instance accounts and other people's."""
    account = await _service.get(db, account_id)
    account = await _service.update(db, account, data.changes(), actor=current_user, request=request)
    return await _one(db, account)


@router.post("/admin/ai-billing-accounts/{account_id}/deactivate/", response_model=AiBillingAccountOut)
async def admin_deactivate_billing_account(
    account_id: int, request: Request, db: DbSession, current_user: User = Depends(require_admin_api)
) -> AiBillingAccountOut:
    """Switch any account off (admin only). Audited for instance accounts and other people's."""
    account = await _service.get(db, account_id)
    account = await _service.deactivate(db, account, actor=current_user, request=request)
    return await _one(db, account)
