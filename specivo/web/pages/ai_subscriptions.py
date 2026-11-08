"""Web pages for AI subscriptions and AI tools.

- ``/my/ai-subscriptions/``: a person's own plans ("My AI subscriptions").
- ``/admin/ai-subscriptions/``: everyone's plans, by user, and the
  self-service switch.
- ``/admin/ai-tools/``: the tool list.

Plain server-rendered forms (no script). A form that fails validation is
rendered again with what was typed and the error; a successful write
redirects with a ``notice`` code. Dates are UTC days.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_subscriptions import CUSTOM_PLAN_KEY, PLAN_PRESETS, get_preset
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.core.i18n import get_locale
from specivo.core.money import currency_display_name, format_decimal_plain
from specivo.core.utils import utcnow
from specivo.models.ai_subscription import AiSubscription, AiTool
from specivo.models.user import User
from specivo.services.ai_subscription_service import AiSubscriptionService, AiToolService, SubscriptionInput
from specivo.services.anonymous_user_service import real_users_clause
from specivo.services.api_key_scope import require_unlimited_key
from specivo.services.currency_service import CurrencyService
from specivo.web.deps import get_templates, require_user
from specivo.web.pages.admin import require_admin

router = APIRouter(tags=["web-ai-subscriptions"], include_in_schema=False)
_service = AiSubscriptionService()
_tools = AiToolService()

MY_URL = "/my/ai-subscriptions/"
ADMIN_URL = "/admin/ai-subscriptions/"
TOOLS_URL = "/admin/ai-tools/"

_NOTICES = frozenset({"created", "saved", "deleted", "self_service_on", "self_service_off", "tool_created"})


class _BadDateError(ValueError):
    pass


def _parse_date(value: str, field_name: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise _BadDateError(field_name) from None


def _redirect(url: str, params: dict[str, str] | None = None) -> RedirectResponse:
    query = urlencode({k: v for k, v in (params or {}).items() if v})
    return RedirectResponse(url + (f"?{query}" if query else ""), 303)


def _status(sub: AiSubscription, today: date) -> str:
    if sub.starts_on > today:
        return "future"
    if sub.ends_on is not None and sub.ends_on < today:
        return "ended"
    return "active"


def subscription_row(sub: AiSubscription, owner: User | None, tool_names: dict[str, str], today: date) -> dict:
    """A subscription for the templates."""
    locale = get_locale()
    return {
        "id": sub.id,
        "user_id": sub.user_id,
        "owner_login": owner.login if owner else "",
        "owner_name": (owner.display_name or owner.login) if owner else "",
        "plan_key": sub.plan_key,
        "plan_name": sub.plan_name,
        "provider": sub.provider,
        "tools": [tool_names.get(s, s) for s in sub.tool_slugs or []],
        "fee": sub.fee.format(locale),
        "billing_period": sub.billing_period,
        "starts_on": sub.starts_on.isoformat(),
        "ends_on": sub.ends_on.isoformat() if sub.ends_on else "",
        "status": _status(sub, today),
        "notes": sub.notes or "",
    }


def _form_from_row(sub: AiSubscription) -> dict[str, Any]:
    return {
        "plan_key": sub.plan_key,
        "provider": sub.provider,
        "plan_name": sub.plan_name,
        "tool_slugs": list(sub.tool_slugs or []),
        "amount": format_decimal_plain(sub.fee.quantize().amount),
        "currency": sub.currency_code,
        "billing_period": sub.billing_period,
        "starts_on": sub.starts_on.isoformat(),
        "ends_on": sub.ends_on.isoformat() if sub.ends_on else "",
        "notes": sub.notes or "",
        "user_id": sub.user_id,
    }


def _form_for_preset(key: str, today: date) -> dict[str, Any]:
    preset = get_preset(key)
    base: dict[str, Any] = {
        "plan_key": CUSTOM_PLAN_KEY,
        "provider": "",
        "plan_name": "",
        "tool_slugs": [],
        "amount": "",
        "currency": "USD",
        "billing_period": "month",
        "starts_on": today.isoformat(),
        "ends_on": "",
        "notes": "",
        "user_id": None,
    }
    if preset is None:
        return base
    return {
        **base,
        "plan_key": preset.key,
        "provider": preset.provider,
        "plan_name": preset.name,
        "tool_slugs": list(preset.tool_slugs),
        "amount": format_decimal_plain(preset.money.quantize().amount),
        "currency": preset.currency,
        "billing_period": preset.billing_period,
    }


async def _choices(db: AsyncSession, form: dict[str, Any]) -> dict[str, Any]:
    """Pickers: plans, tools (active plus those the form already holds) and currencies."""
    locale = get_locale()
    all_tools = await _tools.list_tools(db, active_only=False)
    selected = set(form.get("tool_slugs") or [])
    currencies = await CurrencyService().list_currencies(db, active_only=True, include=form.get("currency") or None)
    return {
        "presets": [
            {
                "key": p.key,
                "name": p.name,
                "hint": p.money.format(locale),
                "period": p.billing_period,
            }
            for p in PLAN_PRESETS
        ],
        "tools": [
            {"slug": t.slug, "name": t.name, "provider": t.default_provider or ""}
            for t in all_tools
            if t.is_active or t.slug in selected
        ],
        "currencies": [
            {"code": c.code, "name": currency_display_name(c.code, locale, fallback=c.name)} for c in currencies
        ],
    }


async def _tool_names(db: AsyncSession) -> dict[str, str]:
    return {t.slug: t.name for t in await _tools.list_tools(db, active_only=False)}


def _read_form(
    *,
    plan_key: str,
    provider: str,
    plan_name: str,
    tool_slugs: list[str],
    amount: str,
    currency: str,
    billing_period: str,
    starts_on: str,
    ends_on: str,
    notes: str,
) -> dict[str, Any]:
    return {
        "plan_key": plan_key.strip() or CUSTOM_PLAN_KEY,
        "provider": provider.strip(),
        "plan_name": plan_name.strip(),
        "tool_slugs": [s for s in tool_slugs if s],
        "amount": amount.strip(),
        "currency": currency.strip().upper(),
        "billing_period": billing_period.strip() or "month",
        "starts_on": starts_on.strip(),
        "ends_on": ends_on.strip(),
        "notes": notes,
    }


def _changes(form: dict[str, Any]) -> dict[str, Any]:
    """Service changes from a submitted form (every field, as an edit replaces them all)."""
    return {
        "plan_key": form["plan_key"],
        "provider": form["provider"],
        "plan_name": form["plan_name"],
        "tool_slugs": form["tool_slugs"],
        "amount": form["amount"],
        "currency": form["currency"],
        "billing_period": form["billing_period"],
        "starts_on": _parse_date(form["starts_on"], "starts_on"),
        "ends_on": _parse_date(form["ends_on"], "ends_on"),
        "notes": form["notes"],
    }


def _error(exc: Exception) -> dict[str, str]:
    if isinstance(exc, _BadDateError):
        return {"code": "date_invalid", "field": str(exc)}
    if isinstance(exc, PermissionDeniedError):
        return {"code": "self_service_off", "field": ""}
    if isinstance(exc, AppError):
        return {"code": exc.code, "field": exc.field or ""}
    raise exc


# ---------------------------------------------------------------------------
# My AI subscriptions
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
    today = utcnow().date()
    rows, _total = await _service.list_subscriptions(db, user_id=user.id, limit=200)
    names = await _tool_names(db)
    return get_templates().TemplateResponse(
        request,
        "pages/auth/ai_subscriptions.html",
        context={
            "user": user,
            "active_page": "ai-subscriptions",
            "subscriptions": [subscription_row(r, user, names, today) for r in rows],
            "can_hold": _service.can_hold(user),
            "can_edit": await _service.can_edit_own(db, user),
            "form": form,
            "form_action": MY_URL,
            "notice": notice if notice in _NOTICES else "",
            "error": error or {},
            **await _choices(db, form),
        },
        status_code=status_code,
    )


@router.get(MY_URL, response_model=None)
async def my_ai_subscriptions(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    preset: str = Query(""),
    notice: str = Query(""),
) -> Response:
    """The signed-in person's AI subscriptions, and the form to add one (``?preset=`` fills it)."""
    require_unlimited_key()
    return await _render_my(request, db, user, form=_form_for_preset(preset, utcnow().date()), notice=notice)


@router.post(MY_URL, response_model=None)
async def my_ai_subscription_create(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    plan_key: str = Form(CUSTOM_PLAN_KEY),
    provider: str = Form(""),
    plan_name: str = Form(""),
    tool_slugs: list[str] = Form([]),  # noqa: B008
    amount: str = Form(""),
    currency: str = Form(""),
    billing_period: str = Form("month"),
    starts_on: str = Form(""),
    ends_on: str = Form(""),
    notes: str = Form(""),
) -> Response:
    require_unlimited_key()
    form = _read_form(
        plan_key=plan_key,
        provider=provider,
        plan_name=plan_name,
        tool_slugs=tool_slugs,
        amount=amount,
        currency=currency,
        billing_period=billing_period,
        starts_on=starts_on,
        ends_on=ends_on,
        notes=notes,
    )
    try:
        changes = _changes(form)
        await _service.create(db, SubscriptionInput(**changes), owner=user, actor=user, request=request)
    except (AppError, _BadDateError) as exc:
        return await _render_my(request, db, user, form=form, error=_error(exc), status_code=422)
    return _redirect(MY_URL, {"notice": "created"})


async def _render_edit(
    request: Request,
    db: AsyncSession,
    user: User,
    sub: AiSubscription,
    *,
    admin_mode: bool,
    form: dict[str, Any] | None = None,
    error: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    owner = await db.get(User, sub.user_id)
    form = form or _form_from_row(sub)
    base = ADMIN_URL if admin_mode else MY_URL
    return get_templates().TemplateResponse(
        request,
        "pages/auth/ai_subscription_edit.html",
        context={
            "user": user,
            "active_page": "admin" if admin_mode else "ai-subscriptions",
            "admin_mode": admin_mode,
            "subscription": subscription_row(sub, owner, await _tool_names(db), utcnow().date()),
            "form": form,
            "form_action": f"{base}{sub.id}/",
            "delete_action": f"{base}{sub.id}/delete/",
            "back_url": f"{ADMIN_URL}?user_id={sub.user_id}" if admin_mode else MY_URL,
            "error": error or {},
            **await _choices(db, form),
        },
        status_code=status_code,
    )


async def _own_or_none(db: AsyncSession, user: User, subscription_id: int) -> AiSubscription | None:
    try:
        return await _service.get_own(db, user, subscription_id)
    except NotFoundError:
        return None


@router.get(MY_URL + "{subscription_id}/", response_model=None)
async def my_ai_subscription_edit(
    subscription_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    require_unlimited_key()
    sub = await _own_or_none(db, user, subscription_id)
    if sub is None:
        raise NotFoundError("AI subscription not found")
    if not await _service.can_edit_own(db, user):
        return _redirect(MY_URL)
    return await _render_edit(request, db, user, sub, admin_mode=False)


@router.post(MY_URL + "{subscription_id}/", response_model=None)
async def my_ai_subscription_update(
    subscription_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    plan_key: str = Form(CUSTOM_PLAN_KEY),
    provider: str = Form(""),
    plan_name: str = Form(""),
    tool_slugs: list[str] = Form([]),  # noqa: B008
    amount: str = Form(""),
    currency: str = Form(""),
    billing_period: str = Form("month"),
    starts_on: str = Form(""),
    ends_on: str = Form(""),
    notes: str = Form(""),
) -> Response:
    require_unlimited_key()
    sub = await _own_or_none(db, user, subscription_id)
    if sub is None:
        raise NotFoundError("AI subscription not found")
    form = _read_form(
        plan_key=plan_key,
        provider=provider,
        plan_name=plan_name,
        tool_slugs=tool_slugs,
        amount=amount,
        currency=currency,
        billing_period=billing_period,
        starts_on=starts_on,
        ends_on=ends_on,
        notes=notes,
    )
    try:
        await _service.update(db, sub, _changes(form), actor=user, request=request)
    except (AppError, _BadDateError) as exc:
        sub = await _service.get(db, subscription_id)
        return await _render_edit(
            request, db, user, sub, admin_mode=False, form=form, error=_error(exc), status_code=422
        )
    return _redirect(MY_URL, {"notice": "saved"})


@router.post(MY_URL + "{subscription_id}/delete/", response_model=None)
async def my_ai_subscription_delete(
    subscription_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    require_unlimited_key()
    sub = await _own_or_none(db, user, subscription_id)
    if sub is None:
        raise NotFoundError("AI subscription not found")
    await _service.delete(db, sub, actor=user, request=request)
    return _redirect(MY_URL, {"notice": "deleted"})


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------


async def _people(db: AsyncSession, include_id: int | None = None) -> list[dict[str, Any]]:
    """People who can hold subscriptions, for the user picker."""
    stmt = (
        select(User)
        .where(real_users_clause(), User.is_service_account.is_(False))
        .where((User.status != "deactivated") | (User.id == (include_id or 0)))
        .order_by(User.login)
    )
    return [
        {"id": u.id, "login": u.login, "name": u.display_name or u.login}
        for u in (await db.execute(stmt)).scalars().all()
    ]


async def _render_admin(
    request: Request,
    db: AsyncSession,
    user: User,
    *,
    user_id: int | None,
    current: str,
    offset: int,
    limit: int,
    form: dict[str, Any],
    notice: str = "",
    error: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    today = utcnow().date()
    rows, total = await _service.list_subscriptions(
        db, user_id=user_id, active_on=today if current == "1" else None, offset=offset, limit=limit
    )
    owners = await _service.owners(db, (r.user_id for r in rows))
    names = await _tool_names(db)
    selected_user = await db.get(User, user_id) if user_id else None
    filters = {"user_id": str(user_id or ""), "current": current}
    return get_templates().TemplateResponse(
        request,
        "pages/admin/ai_subscriptions.html",
        context={
            "user": user,
            "active_page": "admin",
            "subscriptions": [subscription_row(r, owners.get(r.user_id), names, today) for r in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
            "filters": filters,
            "page_query": urlencode({k: v for k, v in filters.items() if v}),
            "selected_user": selected_user,
            "people": await _people(db, include_id=user_id),
            "self_service": await _service.self_service_enabled(db),
            "form": form,
            "form_action": ADMIN_URL,
            "notice": notice if notice in _NOTICES else "",
            "error": error or {},
            **await _choices(db, form),
        },
        status_code=status_code,
    )


@router.get(ADMIN_URL, response_class=HTMLResponse)
async def admin_ai_subscriptions(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    user_id: int | None = Query(None),
    current: str = Query("0"),
    preset: str = Query(""),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    notice: str = Query(""),
) -> Response:
    """Everyone's subscriptions (or one user's), the add form and the self-service switch."""
    form = _form_for_preset(preset, utcnow().date())
    form["user_id"] = user_id
    return await _render_admin(
        request,
        db,
        user,
        user_id=user_id,
        current="1" if current == "1" else "0",
        offset=offset,
        limit=limit,
        form=form,
        notice=notice,
    )


@router.post(ADMIN_URL, response_model=None)
async def admin_ai_subscription_create(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    user_id: str = Form(""),
    plan_key: str = Form(CUSTOM_PLAN_KEY),
    provider: str = Form(""),
    plan_name: str = Form(""),
    tool_slugs: list[str] = Form([]),  # noqa: B008
    amount: str = Form(""),
    currency: str = Form(""),
    billing_period: str = Form("month"),
    starts_on: str = Form(""),
    ends_on: str = Form(""),
    notes: str = Form(""),
) -> Response:
    form = _read_form(
        plan_key=plan_key,
        provider=provider,
        plan_name=plan_name,
        tool_slugs=tool_slugs,
        amount=amount,
        currency=currency,
        billing_period=billing_period,
        starts_on=starts_on,
        ends_on=ends_on,
        notes=notes,
    )
    owner_id = int(user_id) if user_id.strip().isdigit() else None
    form["user_id"] = owner_id
    owner = await db.get(User, owner_id) if owner_id else None
    try:
        if owner is None:
            raise AppError(code="user_not_found", message="Choose a person", status_code=422, field="user_id")
        await _service.create(db, SubscriptionInput(**_changes(form)), owner=owner, actor=user, request=request)
    except (AppError, _BadDateError) as exc:
        return await _render_admin(
            request,
            db,
            user,
            user_id=owner_id,
            current="0",
            offset=0,
            limit=50,
            form=form,
            error=_error(exc),
            status_code=422,
        )
    return _redirect(ADMIN_URL, {"user_id": str(owner.id), "notice": "created"})


@router.post(ADMIN_URL + "settings/", response_model=None)
async def admin_ai_subscription_settings(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    self_service: str = Form(""),
) -> Response:
    """Switch self-service on or off."""
    enabled = self_service == "1"
    await _service.set_self_service(db, enabled, actor=user, request=request)
    return _redirect(ADMIN_URL, {"notice": "self_service_on" if enabled else "self_service_off"})


async def _sub_or_none(db: AsyncSession, subscription_id: int) -> AiSubscription | None:
    try:
        return await _service.get(db, subscription_id)
    except NotFoundError:
        return None


@router.get(ADMIN_URL + "{subscription_id}/", response_model=None)
async def admin_ai_subscription_edit(
    subscription_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    sub = await _sub_or_none(db, subscription_id)
    if sub is None:
        raise NotFoundError("AI subscription not found")
    return await _render_edit(request, db, user, sub, admin_mode=True)


@router.post(ADMIN_URL + "{subscription_id}/", response_model=None)
async def admin_ai_subscription_update(
    subscription_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    plan_key: str = Form(CUSTOM_PLAN_KEY),
    provider: str = Form(""),
    plan_name: str = Form(""),
    tool_slugs: list[str] = Form([]),  # noqa: B008
    amount: str = Form(""),
    currency: str = Form(""),
    billing_period: str = Form("month"),
    starts_on: str = Form(""),
    ends_on: str = Form(""),
    notes: str = Form(""),
) -> Response:
    sub = await _sub_or_none(db, subscription_id)
    if sub is None:
        raise NotFoundError("AI subscription not found")
    form = _read_form(
        plan_key=plan_key,
        provider=provider,
        plan_name=plan_name,
        tool_slugs=tool_slugs,
        amount=amount,
        currency=currency,
        billing_period=billing_period,
        starts_on=starts_on,
        ends_on=ends_on,
        notes=notes,
    )
    form["user_id"] = sub.user_id
    try:
        await _service.update(db, sub, _changes(form), actor=user, request=request)
    except (AppError, _BadDateError) as exc:
        sub = await _service.get(db, subscription_id)
        return await _render_edit(
            request, db, user, sub, admin_mode=True, form=form, error=_error(exc), status_code=422
        )
    return _redirect(ADMIN_URL, {"user_id": str(sub.user_id), "notice": "saved"})


@router.post(ADMIN_URL + "{subscription_id}/delete/", response_model=None)
async def admin_ai_subscription_delete(
    subscription_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    sub = await _sub_or_none(db, subscription_id)
    if sub is None:
        return _redirect(ADMIN_URL)
    owner_id = sub.user_id
    await _service.delete(db, sub, actor=user, request=request)
    return _redirect(ADMIN_URL, {"user_id": str(owner_id), "notice": "deleted"})


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _tool_row(tool: AiTool) -> dict[str, Any]:
    return {
        "slug": tool.slug,
        "name": tool.name,
        "default_provider": tool.default_provider or "",
        "is_active": tool.is_active,
        "position": tool.position,
    }


async def _render_tools(
    request: Request,
    db: AsyncSession,
    user: User,
    *,
    notice: str = "",
    error: dict[str, str] | None = None,
    form: dict[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    return get_templates().TemplateResponse(
        request,
        "pages/admin/ai_tools.html",
        context={
            "user": user,
            "active_page": "admin",
            "tools": [_tool_row(t) for t in await _tools.list_tools(db, active_only=False)],
            "notice": notice if notice in _NOTICES else "",
            "error": error or {},
            "form": form or {},
        },
        status_code=status_code,
    )


@router.get(TOOLS_URL, response_class=HTMLResponse)
async def admin_ai_tools(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    notice: str = Query(""),
) -> Response:
    """The AI tools: rename, set the default provider and order, activate."""
    return await _render_tools(request, db, user, notice=notice)


def _position(value: str) -> int:
    text = value.strip()
    try:
        number = int(text) if text else 0
    except ValueError:
        raise AppError(code="ai_tool_invalid", message="Position", status_code=422, field="position") from None
    if not -100000 <= number <= 100000:
        raise AppError(code="ai_tool_invalid", message="Position", status_code=422, field="position")
    return number


@router.post(TOOLS_URL, response_model=None)
async def admin_ai_tool_create(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    slug: str = Form(""),
    name: str = Form(""),
    default_provider: str = Form(""),
    position: str = Form("0"),
) -> Response:
    form = {"slug": slug, "name": name, "default_provider": default_provider, "position": position}
    try:
        await _tools.create(
            db,
            slug=slug,
            name=name,
            default_provider=default_provider or None,
            is_active=True,
            position=_position(position),
        )
    except AppError as exc:
        return await _render_tools(request, db, user, error=_error(exc), form=form, status_code=422)
    return _redirect(TOOLS_URL, {"notice": "tool_created"})


@router.post(TOOLS_URL + "{slug}/", response_model=None)
async def admin_ai_tool_update(
    slug: str,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    name: str = Form(""),
    default_provider: str = Form(""),
    position: str = Form("0"),
    is_active: str = Form(""),
) -> Response:
    try:
        await _tools.update(
            db,
            slug,
            {
                "name": name,
                "default_provider": default_provider or None,
                "position": _position(position),
                "is_active": is_active == "1",
            },
        )
    except AppError as exc:
        return await _render_tools(request, db, user, error={**_error(exc), "slug": slug}, status_code=422)
    return _redirect(TOOLS_URL, {"notice": "saved"})
