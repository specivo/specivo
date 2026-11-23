"""Web pages for AI billing accounts.

- ``/my/ai-billing-accounts/``: the company accounts a person may name in a
  reporter's configuration (read-only), and their own personal accounts,
  with the reimbursement share, next to "My AI subscriptions".
- ``/admin/ai-billing-accounts/``: the instance (company) accounts: corporate
  gateways and company plans. Administrators also open anyone's personal
  account by its id (from the subscriptions page).

Plain server-rendered forms (no script), like the subscription pages. A form
that fails validation is rendered again with what was typed and the error; a
successful write redirects with a ``notice`` code. Accounts are deactivated,
never deleted.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_billing import (
    ACCOUNT_DEFAULT_COST_TYPES,
    ACCOUNT_PAYERS,
    BILLING_ACCOUNT_KINDS,
    PAYER_EMPLOYEE,
    PAYER_EMPLOYER,
)
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.user import User
from specivo.schemas.ai_billing_account import share_text
from specivo.services.ai_billing_account_service import AiBillingAccountService, BillingAccountInput
from specivo.services.ai_subscription_service import AiSubscriptionService, AiToolService
from specivo.services.api_key_scope import require_unlimited_key
from specivo.web.deps import get_templates, redirect_to, require_user
from specivo.web.pages.admin import require_admin

router = APIRouter(tags=["web-ai-billing-accounts"], include_in_schema=False)
_service = AiBillingAccountService()
_subscriptions = AiSubscriptionService()
_tools = AiToolService()

MY_URL = "/my/ai-billing-accounts/"
ADMIN_URL = "/admin/ai-billing-accounts/"

_NOTICES = frozenset({"created", "saved", "deactivated"})


def _redirect(url: str, params: dict[str, str] | None = None) -> RedirectResponse:
    query = urlencode({k: v for k, v in (params or {}).items() if v})
    return redirect_to(url + (f"?{query}" if query else ""), 303)


def account_row(account: AiBillingAccount, owner: User | None, tool_names: dict[str, str]) -> dict[str, Any]:
    """An account for the templates."""
    share = account.reimburse_share
    return {
        "id": account.id,
        "slug": account.slug,
        "name": account.name,
        "kind": account.kind,
        "is_instance": account.owner_user_id is None,
        "owner_user_id": account.owner_user_id,
        "owner_login": owner.login if owner else "",
        "owner_name": (owner.display_name or owner.login) if owner else "",
        "payer": account.payer,
        "reimburse_share": share_text(share),
        "reimburse_percent": format((share * 100).normalize(), "f") if isinstance(share, Decimal) else "",
        "route_patterns": list(account.route_patterns or []),
        "tools": [tool_names.get(s, s) for s in account.tool_slugs or []],
        "default_cost_type": account.default_cost_type or "",
        "is_active": account.is_active,
    }


def _form_from_row(account: AiBillingAccount) -> dict[str, Any]:
    return {
        "slug": account.slug,
        "name": account.name,
        "kind": account.kind,
        "payer": account.payer,
        "reimburse_share": share_text(account.reimburse_share) or "",
        "route_patterns": " ".join(account.route_patterns or []),
        "tool_slugs": list(account.tool_slugs or []),
        "default_cost_type": account.default_cost_type or "",
        "is_active": account.is_active,
    }


def _blank_form(*, personal: bool) -> dict[str, Any]:
    return {
        "slug": "",
        "name": "",
        "kind": "api_key" if personal else "gateway",
        "payer": PAYER_EMPLOYEE if personal else PAYER_EMPLOYER,
        "reimburse_share": "",
        "route_patterns": "",
        "tool_slugs": [],
        "default_cost_type": "api",
        "is_active": True,
    }


def _read_form(
    *,
    slug: str,
    name: str,
    kind: str,
    payer: str,
    reimburse_share: str,
    route_patterns: str,
    tool_slugs: list[str],
    default_cost_type: str,
    is_active: bool,
) -> dict[str, Any]:
    return {
        "slug": slug.strip(),
        "name": name.strip(),
        "kind": kind.strip(),
        "payer": payer.strip(),
        "reimburse_share": reimburse_share.strip(),
        "route_patterns": route_patterns,
        "tool_slugs": [s for s in tool_slugs if s],
        "default_cost_type": default_cost_type.strip(),
        "is_active": is_active,
    }


def _changes(form: dict[str, Any]) -> dict[str, Any]:
    """Service changes from a submitted form (every field, as an edit replaces them all).

    A share left on an employer-paid account is dropped rather than refused:
    the field is simply not shown for those.
    """
    changes = dict(form)
    if changes["payer"] != PAYER_EMPLOYEE:
        changes["reimburse_share"] = None
    return changes


def _error(exc: Exception) -> dict[str, str]:
    if isinstance(exc, PermissionDeniedError):
        return {"code": "self_service_off", "field": ""}
    if isinstance(exc, AppError):
        return {"code": exc.code, "field": exc.field or ""}
    raise exc


async def _choices(db: AsyncSession, form: dict[str, Any]) -> dict[str, Any]:
    """Pickers: kinds, payers, cost types, and the tools (active plus those the form already holds)."""
    selected = set(form.get("tool_slugs") or [])
    return {
        "kinds": list(BILLING_ACCOUNT_KINDS),
        "payers": list(ACCOUNT_PAYERS),
        "cost_types": list(ACCOUNT_DEFAULT_COST_TYPES),
        "tools": [
            {"slug": t.slug, "name": t.name}
            for t in await _tools.list_tools(db, active_only=False)
            if t.is_active or t.slug in selected
        ],
    }


async def _tool_names(db: AsyncSession) -> dict[str, str]:
    return {t.slug: t.name for t in await _tools.list_tools(db, active_only=False)}


async def _links(db: AsyncSession, rows: list[AiBillingAccount]) -> dict[int, list[int]]:
    return await _service.subscription_links(db, (r.id for r in rows))


# ---------------------------------------------------------------------------
# My AI billing accounts
# ---------------------------------------------------------------------------


async def _render_my(
    request: Request,
    db: AsyncSession,
    user: User,
    *,
    form: dict[str, Any],
    notice: str = "",
    error: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    names = await _tool_names(db)
    instance = await _service.instance_accounts(db, active_only=True)
    own = await _service.personal_accounts(db, user.id, active_only=False)
    links = await _links(db, own)
    return get_templates().TemplateResponse(
        request,
        "pages/auth/ai_billing_accounts.html",
        context={
            "user": user,
            "active_page": "ai-billing-accounts",
            "instance_accounts": [account_row(a, None, names) for a in instance],
            "accounts": [{**account_row(a, user, names), "subscription_ids": links.get(a.id, [])} for a in own],
            "can_hold": _subscriptions.can_hold(user),
            "can_edit": await _service.can_manage(db, user, user.id) and _subscriptions.can_hold(user),
            "form": form,
            "form_action": MY_URL,
            "notice": notice if notice in _NOTICES else "",
            "error": error or {},
            **await _choices(db, form),
        },
        status_code=status_code,
    )


@router.get(MY_URL, response_model=None)
async def my_ai_billing_accounts(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    notice: str = Query(""),
) -> Response:
    """The company accounts and the signed-in person's own accounts, and the form to add one."""
    require_unlimited_key()
    return await _render_my(request, db, user, form=_blank_form(personal=True), notice=notice)


@router.post(MY_URL, response_model=None)
async def my_ai_billing_account_create(  # noqa: PLR0913
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    slug: str = Form(""),
    name: str = Form(""),
    kind: str = Form(""),
    payer: str = Form(""),
    reimburse_share: str = Form(""),
    route_patterns: str = Form(""),
    tool_slugs: list[str] = Form([]),
    default_cost_type: str = Form(""),
) -> Response:
    require_unlimited_key()
    form = _read_form(
        slug=slug,
        name=name,
        kind=kind,
        payer=payer,
        reimburse_share=reimburse_share,
        route_patterns=route_patterns,
        tool_slugs=tool_slugs,
        default_cost_type=default_cost_type,
        is_active=True,
    )
    try:
        await _service.create(db, BillingAccountInput(**_changes(form)), owner=user, actor=user, request=request)
    except AppError as exc:
        return await _render_my(request, db, user, form=form, error=_error(exc), status_code=422)
    return _redirect(MY_URL, {"notice": "created"})


async def _render_edit(
    request: Request,
    db: AsyncSession,
    user: User,
    account: AiBillingAccount,
    *,
    admin_mode: bool,
    form: dict[str, Any] | None = None,
    error: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    owner = await db.get(User, account.owner_user_id) if account.owner_user_id is not None else None
    form = form or _form_from_row(account)
    base = ADMIN_URL if admin_mode else MY_URL
    links = await _links(db, [account])
    return get_templates().TemplateResponse(
        request,
        "pages/auth/ai_billing_account_edit.html",
        context={
            "user": user,
            "active_page": "admin" if admin_mode else "ai-billing-accounts",
            "admin_mode": admin_mode,
            "account": {
                **account_row(account, owner, await _tool_names(db)),
                "subscription_ids": links.get(account.id, []),
            },
            "form": form,
            "form_action": f"{base}{account.id}/",
            "deactivate_action": f"{base}{account.id}/deactivate/",
            "back_url": base,
            "error": error or {},
            **await _choices(db, form),
        },
        status_code=status_code,
    )


async def _own_or_404(db: AsyncSession, user: User, account_id: int) -> AiBillingAccount:
    """*user*'s own account; 404 for an instance account or anybody else's."""
    account = await _service.get(db, account_id)
    if account.owner_user_id != user.id:
        raise NotFoundError("AI billing account not found")
    return account


@router.get(MY_URL + "{account_id}/", response_model=None)
async def my_ai_billing_account_edit(
    account_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    require_unlimited_key()
    account = await _own_or_404(db, user, account_id)
    if not await _service.can_manage(db, user, user.id):
        return _redirect(MY_URL)
    return await _render_edit(request, db, user, account, admin_mode=False)


@router.post(MY_URL + "{account_id}/", response_model=None)
async def my_ai_billing_account_update(  # noqa: PLR0913
    account_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    slug: str = Form(""),
    name: str = Form(""),
    kind: str = Form(""),
    payer: str = Form(""),
    reimburse_share: str = Form(""),
    route_patterns: str = Form(""),
    tool_slugs: list[str] = Form([]),
    default_cost_type: str = Form(""),
    is_active: str = Form(""),
) -> Response:
    require_unlimited_key()
    account = await _own_or_404(db, user, account_id)
    form = _read_form(
        slug=slug,
        name=name,
        kind=kind,
        payer=payer,
        reimburse_share=reimburse_share,
        route_patterns=route_patterns,
        tool_slugs=tool_slugs,
        default_cost_type=default_cost_type,
        is_active=is_active == "1",
    )
    try:
        await _service.update(db, account, _changes(form), actor=user, request=request)
    except AppError as exc:
        account = await _service.get(db, account_id)
        return await _render_edit(
            request, db, user, account, admin_mode=False, form=form, error=_error(exc), status_code=422
        )
    return _redirect(MY_URL, {"notice": "saved"})


@router.post(MY_URL + "{account_id}/deactivate/", response_model=None)
async def my_ai_billing_account_deactivate(
    account_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    require_unlimited_key()
    account = await _own_or_404(db, user, account_id)
    await _service.deactivate(db, account, actor=user, request=request)
    return _redirect(MY_URL, {"notice": "deactivated"})


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------


async def _render_admin(
    request: Request,
    db: AsyncSession,
    user: User,
    *,
    form: dict[str, Any],
    notice: str = "",
    error: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    names = await _tool_names(db)
    rows, _total = await _service.list_accounts(db, scope="instance", limit=200)
    return get_templates().TemplateResponse(
        request,
        "pages/admin/ai_billing_accounts.html",
        context={
            "user": user,
            "active_page": "admin",
            "accounts": [account_row(a, None, names) for a in rows],
            "form": form,
            "form_action": ADMIN_URL,
            "notice": notice if notice in _NOTICES else "",
            "error": error or {},
            **await _choices(db, form),
        },
        status_code=status_code,
    )


@router.get(ADMIN_URL, response_class=HTMLResponse)
async def admin_ai_billing_accounts(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    notice: str = Query(""),
) -> Response:
    """The instance (company) accounts and the form to add one."""
    return await _render_admin(request, db, user, form=_blank_form(personal=False), notice=notice)


@router.post(ADMIN_URL, response_model=None)
async def admin_ai_billing_account_create(  # noqa: PLR0913
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    slug: str = Form(""),
    name: str = Form(""),
    kind: str = Form(""),
    payer: str = Form(""),
    reimburse_share: str = Form(""),
    route_patterns: str = Form(""),
    tool_slugs: list[str] = Form([]),
    default_cost_type: str = Form(""),
) -> Response:
    form = _read_form(
        slug=slug,
        name=name,
        kind=kind,
        payer=payer,
        reimburse_share=reimburse_share,
        route_patterns=route_patterns,
        tool_slugs=tool_slugs,
        default_cost_type=default_cost_type,
        is_active=True,
    )
    try:
        await _service.create(db, BillingAccountInput(**_changes(form)), owner=None, actor=user, request=request)
    except AppError as exc:
        return await _render_admin(request, db, user, form=form, error=_error(exc), status_code=422)
    return _redirect(ADMIN_URL, {"notice": "created"})


@router.get(ADMIN_URL + "{account_id}/", response_model=None)
async def admin_ai_billing_account_edit(
    account_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    account = await _service.get(db, account_id)
    return await _render_edit(request, db, user, account, admin_mode=True)


@router.post(ADMIN_URL + "{account_id}/", response_model=None)
async def admin_ai_billing_account_update(  # noqa: PLR0913
    account_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    slug: str = Form(""),
    name: str = Form(""),
    kind: str = Form(""),
    payer: str = Form(""),
    reimburse_share: str = Form(""),
    route_patterns: str = Form(""),
    tool_slugs: list[str] = Form([]),
    default_cost_type: str = Form(""),
    is_active: str = Form(""),
) -> Response:
    account = await _service.get(db, account_id)
    form = _read_form(
        slug=slug,
        name=name,
        kind=kind,
        payer=payer,
        reimburse_share=reimburse_share,
        route_patterns=route_patterns,
        tool_slugs=tool_slugs,
        default_cost_type=default_cost_type,
        is_active=is_active == "1",
    )
    try:
        await _service.update(db, account, _changes(form), actor=user, request=request)
    except AppError as exc:
        account = await _service.get(db, account_id)
        return await _render_edit(
            request, db, user, account, admin_mode=True, form=form, error=_error(exc), status_code=422
        )
    return _redirect(ADMIN_URL, {"notice": "saved"})


@router.post(ADMIN_URL + "{account_id}/deactivate/", response_model=None)
async def admin_ai_billing_account_deactivate(
    account_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    account = await _service.get(db, account_id)
    await _service.deactivate(db, account, actor=user, request=request)
    return _redirect(ADMIN_URL, {"notice": "deactivated"})
