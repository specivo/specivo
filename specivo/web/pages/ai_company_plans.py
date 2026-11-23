"""Web pages for company seat plans (administrators).

- ``/admin/ai-company-plans/``: the plans the employer pays for, and the add form.
- ``/admin/ai-company-plans/{id}/``: one plan: its fields, its seats (who holds
  one, by date), add a member, end or remove a seat, delete the plan.

Plain server-rendered forms, like the subscription pages
(:mod:`specivo.web.pages.ai_subscriptions`): a form that fails validation is
rendered again with what was typed and the error; a successful write redirects
with a ``notice`` code. Dates are UTC days. A member is named by login (a text
field), never picked from a list of every user.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_subscriptions import CUSTOM_PLAN_KEY
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import AppError, NotFoundError
from specivo.core.utils import utcnow
from specivo.models.ai_subscription import AiSubscription, AiSubscriptionMember
from specivo.models.user import User
from specivo.services.ai_billing_account_service import AiBillingAccountService
from specivo.services.ai_subscription_service import AiSubscriptionService, SubscriptionInput
from specivo.services.ai_usage_service import AiUsageService
from specivo.web.deps import get_templates
from specivo.web.pages.admin import require_admin
from specivo.web.pages.ai_subscriptions import (
    _BadDateError,
    _changes,
    _choices,
    _error,
    _form_from_row,
    _parse_date,
    _read_form,
    _redirect,
    _status,
    _tool_names,
    subscription_row,
)

router = APIRouter(tags=["web-ai-company-plans"], include_in_schema=False)
_service = AiSubscriptionService()
_accounts = AiBillingAccountService()

PLANS_URL = "/admin/ai-company-plans/"

_NOTICES = frozenset({"created", "saved", "deleted", "member_added", "member_ended", "member_removed", "member_saved"})


def _company_form(today: date) -> dict[str, Any]:
    return {
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
        "seat_count": "",
        "user_id": None,
    }


def _held_today(members: list[AiSubscriptionMember], today: date) -> int:
    return sum(1 for m in members if m.starts_on <= today and (m.ends_on is None or m.ends_on >= today))


async def _plan_or_none(db: AsyncSession, subscription_id: int) -> AiSubscription | None:
    try:
        return await _service.get_company(db, subscription_id)
    except NotFoundError:
        return None


async def _plan_or_404(db: AsyncSession, subscription_id: int) -> AiSubscription:
    sub = await _plan_or_none(db, subscription_id)
    if sub is None:
        raise NotFoundError("Company AI plan not found")
    return sub


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


async def _render_list(
    request: Request,
    db: AsyncSession,
    user: User,
    *,
    form: dict[str, Any],
    offset: int = 0,
    limit: int = 50,
    notice: str = "",
    error: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    today = utcnow().date()
    rows, total = await _service.list_subscriptions(db, company=True, offset=offset, limit=limit)
    names = await _tool_names(db)
    accounts = await _accounts.by_ids(db, (r.billing_account_id for r in rows))
    members = await _service.members_of(db, (r.id for r in rows))
    plans = []
    for row in rows:
        item = subscription_row(row, None, names, today, accounts)
        item["seat_count"] = row.seat_count or 0
        item["seats_held"] = _held_today(members.get(row.id, []), today)
        plans.append(item)
    return get_templates().TemplateResponse(
        request,
        "pages/admin/ai_company_plans.html",
        context={
            "user": user,
            "active_page": "admin",
            "plans": plans,
            "total": total,
            "offset": offset,
            "limit": limit,
            "form": form,
            "form_action": PLANS_URL,
            "show_seats": True,
            "notice": notice if notice in _NOTICES else "",
            "error": error or {},
            **await _choices(db, form),
        },
        status_code=status_code,
    )


@router.get(PLANS_URL, response_class=HTMLResponse)
async def admin_ai_company_plans(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    notice: str = Query(""),
) -> Response:
    """The company seat plans and the add form."""
    return await _render_list(
        request, db, user, form=_company_form(utcnow().date()), offset=offset, limit=limit, notice=notice
    )


@router.post(PLANS_URL, response_model=None)
async def admin_ai_company_plan_create(  # noqa: PLR0913
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    plan_key: str = Form(CUSTOM_PLAN_KEY),
    provider: str = Form(""),
    plan_name: str = Form(""),
    tool_slugs: list[str] = Form([]),
    amount: str = Form(""),
    currency: str = Form(""),
    billing_period: str = Form("month"),
    starts_on: str = Form(""),
    ends_on: str = Form(""),
    notes: str = Form(""),
    seat_count: str = Form(""),
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
    form["seat_count"] = seat_count.strip()
    try:
        sub = await _service.create(
            db,
            SubscriptionInput(**_changes(form), seat_count=form["seat_count"] or None),
            owner=None,
            actor=user,
            request=request,
        )
    except (AppError, _BadDateError) as exc:
        return await _render_list(request, db, user, form=form, error=_error(exc), status_code=422)
    return _redirect(f"{PLANS_URL}{sub.id}/", {"notice": "created"})


# ---------------------------------------------------------------------------
# One plan and its seats
# ---------------------------------------------------------------------------


async def _render_plan(
    request: Request,
    db: AsyncSession,
    user: User,
    sub: AiSubscription,
    *,
    form: dict[str, Any] | None = None,
    member_form: dict[str, str] | None = None,
    notice: str = "",
    error: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    today = utcnow().date()
    form = form or {**_form_from_row(sub), "seat_count": str(sub.seat_count or "")}
    members = await _service.list_members(db, sub)
    people = await _service.owners(db, (m.user_id for m in members))
    accounts = await _accounts.by_ids(db, [sub.billing_account_id])
    plan = subscription_row(sub, None, await _tool_names(db), today, accounts)
    plan["seat_count"] = sub.seat_count or 0
    plan["seats_held"] = _held_today(members, today)
    seats = []
    for member in sorted(members, key=lambda m: (m.starts_on, m.id), reverse=True):
        person = people.get(member.user_id)
        seats.append(
            {
                "id": member.id,
                "login": person.login if person else str(member.user_id),
                "name": (person.display_name or person.login) if person else "",
                "starts_on": member.starts_on.isoformat(),
                "ends_on": member.ends_on.isoformat() if member.ends_on else "",
                "status": _status(member, today),
            }
        )
    return get_templates().TemplateResponse(
        request,
        "pages/admin/ai_company_plan.html",
        context={
            "user": user,
            "active_page": "admin",
            "plan": plan,
            "seats": seats,
            "form": form,
            "form_action": f"{PLANS_URL}{sub.id}/",
            "show_seats": True,
            "member_form": member_form or {"login": "", "starts_on": today.isoformat(), "ends_on": ""},
            "today": today.isoformat(),
            "notice": notice if notice in _NOTICES else "",
            "error": error or {},
            **await _choices(db, form),
        },
        status_code=status_code,
    )


@router.get(PLANS_URL + "{subscription_id}/", response_model=None)
async def admin_ai_company_plan(
    subscription_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    notice: str = Query(""),
) -> Response:
    return await _render_plan(request, db, user, await _plan_or_404(db, subscription_id), notice=notice)


@router.post(PLANS_URL + "{subscription_id}/", response_model=None)
async def admin_ai_company_plan_update(  # noqa: PLR0913
    subscription_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    plan_key: str = Form(CUSTOM_PLAN_KEY),
    provider: str = Form(""),
    plan_name: str = Form(""),
    tool_slugs: list[str] = Form([]),
    amount: str = Form(""),
    currency: str = Form(""),
    billing_period: str = Form("month"),
    starts_on: str = Form(""),
    ends_on: str = Form(""),
    notes: str = Form(""),
    seat_count: str = Form(""),
) -> Response:
    sub = await _plan_or_404(db, subscription_id)
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
    form["seat_count"] = seat_count.strip()
    try:
        changes = _changes(form)
        if form["seat_count"]:
            changes["seat_count"] = form["seat_count"]
        await _service.update(db, sub, changes, actor=user, request=request)
    except (AppError, _BadDateError) as exc:
        sub = await _plan_or_404(db, subscription_id)
        return await _render_plan(request, db, user, sub, form=form, error=_error(exc), status_code=422)
    return _redirect(f"{PLANS_URL}{sub.id}/", {"notice": "saved"})


@router.post(PLANS_URL + "{subscription_id}/delete/", response_model=None)
async def admin_ai_company_plan_delete(
    subscription_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    sub = await _plan_or_none(db, subscription_id)
    if sub is not None:
        await _service.delete(db, sub, actor=user, request=request)
    return _redirect(PLANS_URL, {"notice": "deleted"})


@router.post(PLANS_URL + "{subscription_id}/members/", response_model=None)
async def admin_ai_company_plan_add_member(
    subscription_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    login: str = Form(""),
    starts_on: str = Form(""),
    ends_on: str = Form(""),
) -> Response:
    """Give the person *login* a seat."""
    sub = await _plan_or_404(db, subscription_id)
    member_form = {"login": login.strip(), "starts_on": starts_on.strip(), "ends_on": ends_on.strip()}
    try:
        person = await AiUsageService.user_by_login(db, login) if login.strip() else None
        if person is None:
            raise AppError(code="user_not_found", message="No such person", status_code=422, field="login")
        await _service.add_member(
            db,
            sub,
            person,
            starts_on=_parse_date(starts_on, "starts_on") or utcnow().date(),
            ends_on=_parse_date(ends_on, "ends_on"),
            actor=user,
            request=request,
        )
    except (AppError, _BadDateError) as exc:
        return await _render_plan(request, db, user, sub, member_form=member_form, error=_error(exc), status_code=422)
    return _redirect(f"{PLANS_URL}{sub.id}/", {"notice": "member_added"})


@router.post(PLANS_URL + "{subscription_id}/members/{member_id}/", response_model=None)
async def admin_ai_company_plan_member_update(
    subscription_id: int,
    member_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    ends_on: str = Form(""),
) -> Response:
    """Set a seat's last day (empty reopens it)."""
    sub = await _plan_or_404(db, subscription_id)
    try:
        member = await _service.get_member(db, sub, member_id)
        await _service.update_member(
            db, sub, member, {"ends_on": _parse_date(ends_on, "ends_on")}, actor=user, request=request
        )
    except (AppError, _BadDateError) as exc:
        return await _render_plan(request, db, user, sub, error=_error(exc), status_code=422)
    return _redirect(f"{PLANS_URL}{sub.id}/", {"notice": "member_ended" if ends_on.strip() else "member_saved"})


@router.post(PLANS_URL + "{subscription_id}/members/{member_id}/delete/", response_model=None)
async def admin_ai_company_plan_member_delete(
    subscription_id: int,
    member_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """Remove a seat entered by mistake."""
    sub = await _plan_or_404(db, subscription_id)
    member = await _service.get_member(db, sub, member_id)
    await _service.remove_member(db, sub, member, actor=user, request=request)
    return _redirect(f"{PLANS_URL}{sub.id}/", {"notice": "member_removed"})
