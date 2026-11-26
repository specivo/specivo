"""Web pages for AI reimbursement claims.

- ``/my/ai-reimbursement-claims/``: a person's claims and the form to claim a
  month that has ended; ``/my/ai-reimbursement-claims/{id}/`` one claim, with
  refresh, submit, withdraw and delete, and its CSV export.
- ``/admin/ai-reimbursement-claims/``: everyone's claims (submitted ones
  first by default); ``/admin/ai-reimbursement-claims/{id}/`` one claim, with
  approve, reject and mark paid, and its CSV export.

Plain server-rendered forms (no script). Every action redirects back to the
claim with a ``notice`` code, or with an ``error`` code when the service
refused it. The rules are :class:`AiReimbursementClaimService`'s.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import DB_SESSION
from specivo.core.exceptions import AppError, NotFoundError
from specivo.core.i18n import format_date, format_datetime_for, get_locale
from specivo.core.money import Money
from specivo.core.utils import utcnow
from specivo.models.ai_reimbursement_claim import CLAIM_STATUSES, CLAIM_SUBMITTED, AiReimbursementClaim
from specivo.models.user import User
from specivo.schemas.ai_billing_account import share_text
from specivo.services.ai_reimbursement_claim_service import (
    AiReimbursementClaimService,
    ClaimChange,
    ClaimReferences,
    month_text,
)
from specivo.services.api_key_scope import require_unlimited_key
from specivo.web.deps import get_templates, redirect_to, require_user
from specivo.web.pages.admin import require_admin

router = APIRouter(tags=["web-ai-reimbursement-claims"], include_in_schema=False)
_service = AiReimbursementClaimService()

MY_URL = "/my/ai-reimbursement-claims/"
ADMIN_URL = "/admin/ai-reimbursement-claims/"

_NOTICES = frozenset({"created", "refreshed", "submitted", "withdrawn", "deleted", "approved", "rejected", "paid"})
_HOLDER_ACTIONS = ("refresh", "submit", "withdraw", "delete")
_ADMIN_ACTIONS = ("approve", "reject", "paid")
#: How many past months the claim form offers.
_MONTH_CHOICES = 12


def _redirect(url: str, params: dict[str, str] | None = None) -> RedirectResponse:
    query = urlencode({k: v for k, v in (params or {}).items() if v})
    return redirect_to(url + (f"?{query}" if query else ""), 303)


def _money(amount: Any, currency: str, locale: str) -> str:
    return Money(amount, currency).format(locale)


def _when(value: datetime | None, viewer: User, locale: str) -> str:
    return format_datetime_for(value, viewer.timezone, locale) if value is not None else ""


def _person(user_id: int | None, refs: ClaimReferences) -> str:
    if user_id is None:
        return ""
    user = refs.users.get(user_id)
    return (user.display_name or user.login) if user is not None else ""


def claim_row(
    claim: AiReimbursementClaim, refs: ClaimReferences, changes: list[ClaimChange], viewer: User, locale: str
) -> dict[str, Any]:
    holder = refs.users.get(claim.user_id)
    return {
        "id": claim.id,
        "month": month_text(claim.month),
        "month_label": format_date(claim.month, "yMMMM", locale),
        "status": claim.status,
        "total": claim.total.format(locale),
        "currency": claim.total_currency,
        "entry_count": claim.entry_count,
        "note": claim.note or "",
        "holder_name": (holder.display_name or holder.login) if holder is not None else "",
        "holder_login": holder.login if holder is not None else "",
        "computed_at": _when(claim.computed_at, viewer, locale),
        "submitted_at": _when(claim.submitted_at, viewer, locale),
        "decided_at": _when(claim.decided_at, viewer, locale),
        "decided_by": _person(claim.decided_by_id, refs),
        "decision_note": claim.decision_note or "",
        "paid_at": _when(claim.paid_at, viewer, locale),
        "paid_by": _person(claim.paid_by_id, refs),
        "changed_entries": len(changes),
    }


def _month_choices(today: date) -> list[dict[str, str]]:
    """The months that have ended, newest first."""
    year, month = today.year, today.month
    out: list[dict[str, str]] = []
    locale = get_locale()
    for _i in range(_MONTH_CHOICES):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        first = date(year, month, 1)
        out.append({"value": month_text(first), "label": format_date(first, "yMMMM", locale)})
    return out


def _error(exc: AppError) -> str:
    return exc.code


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


@router.get(MY_URL, response_model=None)
async def my_claims(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    notice: str = Query(""),
    error: str = Query(""),
) -> Response:
    """The signed-in person's claims and the form to claim a month."""
    require_unlimited_key()
    locale = get_locale()
    claims = await _service.list_own(db, user)
    refs = await _service.references(db, claims, [])
    changes = await _service.changes(db, (c.id for c in claims))
    return get_templates().TemplateResponse(
        request,
        "pages/auth/ai_reimbursement_claims.html",
        context={
            "user": user,
            "active_page": "ai-reimbursement-claims",
            "claims": [claim_row(c, refs, changes.get(c.id, []), user, locale) for c in claims],
            "can_hold": _service.can_hold(user),
            "months": _month_choices(utcnow().date()),
            "notice": notice if notice in _NOTICES else "",
            "error": error[:64],
        },
    )


@router.post(MY_URL, response_model=None)
async def my_claim_create(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    month: str = Form(""),
    note: str = Form(""),
) -> Response:
    require_unlimited_key()
    try:
        claim = await _service.create(db, user, month, note=note)
    except AppError as exc:
        return _redirect(MY_URL, {"error": _error(exc)})
    return _redirect(f"{MY_URL}{claim.id}/", {"notice": "created"})


@router.get(ADMIN_URL, response_model=None)
async def admin_claims(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    status: str = Query(CLAIM_SUBMITTED),
    offset: int = Query(0, ge=0),
) -> Response:
    """Everyone's claims of one status (submitted by default; ``all`` for every status)."""
    require_unlimited_key()
    locale = get_locale()
    wanted = status if status in CLAIM_STATUSES else None
    limit = 50
    claims, total = await _service.list_claims(db, user, status=wanted, offset=offset, limit=limit)
    refs = await _service.references(db, claims, [])
    changes = await _service.changes(db, (c.id for c in claims))
    return get_templates().TemplateResponse(
        request,
        "pages/admin/ai_reimbursement_claims.html",
        context={
            "user": user,
            "active_page": "admin",
            "claims": [claim_row(c, refs, changes.get(c.id, []), user, locale) for c in claims],
            "statuses": list(CLAIM_STATUSES),
            "status": wanted or "all",
            "total": total,
            "offset": offset,
            "limit": limit,
        },
    )


# ---------------------------------------------------------------------------
# One claim
# ---------------------------------------------------------------------------


async def _render_claim(
    request: Request,
    db: AsyncSession,
    user: User,
    claim: AiReimbursementClaim,
    *,
    admin_mode: bool,
    notice: str,
    error: str,
) -> Response:
    locale = get_locale()
    entries = await _service.entries(db, claim)
    refs = await _service.references(db, [claim], entries)
    changes = (await _service.changes(db, [claim.id])).get(claim.id, [])
    reasons = {c.claim_entry_id: c.reason for c in changes}
    base = ADMIN_URL if admin_mode else MY_URL
    rows = [
        {
            "usage_date": format_date(e.usage_date, "yMMMd", locale),
            "project": refs.projects.get(e.project_id, "") if e.project_id is not None else "",
            "account": refs.accounts.get(e.billing_account_id, "") if e.billing_account_id is not None else "",
            "tool": e.tool_slug,
            "model": e.model,
            "cost_type": e.cost_type,
            "share_percent": format((e.share * 100).normalize(), "f"),
            "marginal": _money(e.marginal_amount, e.currency, locale),
            "seat": _money(e.seat_amount, e.currency, locale),
            "reimbursable": _money(e.reimbursable_amount, e.currency, locale),
            "changed": reasons.get(e.id, ""),
            "share": share_text(e.share),
        }
        for e in entries
    ]
    return get_templates().TemplateResponse(
        request,
        "pages/auth/ai_reimbursement_claim.html",
        context={
            "user": user,
            "active_page": "admin" if admin_mode else "ai-reimbursement-claims",
            "admin_mode": admin_mode,
            "claim": claim_row(claim, refs, changes, user, locale),
            "entries": rows,
            "is_holder": claim.user_id == user.id,
            "can_decide": _service.can_decide(user, claim),
            "action_base": f"{base}{claim.id}/",
            "back_url": base,
            "export_url": f"{base}{claim.id}/export/",
            "notice": notice if notice in _NOTICES else "",
            "error": error[:64],
        },
    )


async def _own(db: AsyncSession, user: User, claim_id: int) -> AiReimbursementClaim:
    claim = await _service.get_visible(db, user, claim_id)
    if claim.user_id != user.id:
        raise NotFoundError("AI reimbursement claim not found")
    return claim


@router.get(MY_URL + "{claim_id}/", response_model=None)
async def my_claim(
    claim_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    notice: str = Query(""),
    error: str = Query(""),
) -> Response:
    claim = await _own(db, user, claim_id)
    return await _render_claim(request, db, user, claim, admin_mode=False, notice=notice, error=error)


@router.post(MY_URL + "{claim_id}/{action}/", response_model=None)
async def my_claim_action(
    claim_id: int,
    action: str,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    note: str | None = Form(None),
) -> Response:
    """Refresh, submit, withdraw or delete one of the signed-in person's claims."""
    if action not in _HOLDER_ACTIONS:
        raise NotFoundError("Unknown action")
    claim = await _own(db, user, claim_id)
    url = f"{MY_URL}{claim.id}/"
    try:
        if action == "refresh":
            await _service.refresh(db, user, claim, note=note)
            notice = "refreshed"
        elif action == "submit":
            await _service.submit(db, user, claim, request=request)
            notice = "submitted"
        elif action == "withdraw":
            await _service.withdraw(db, user, claim, request=request)
            notice = "withdrawn"
        else:
            await _service.delete(db, user, claim)
            return _redirect(MY_URL, {"notice": "deleted"})
    except AppError as exc:
        return _redirect(url, {"error": _error(exc)})
    return _redirect(url, {"notice": notice})


@router.get(ADMIN_URL + "{claim_id}/", response_model=None)
async def admin_claim(
    claim_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    notice: str = Query(""),
    error: str = Query(""),
) -> Response:
    claim = await _service.get_visible(db, user, claim_id)
    return await _render_claim(request, db, user, claim, admin_mode=True, notice=notice, error=error)


@router.post(ADMIN_URL + "{claim_id}/{action}/", response_model=None)
async def admin_claim_action(
    claim_id: int,
    action: str,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    reason: str = Form(""),
) -> Response:
    """Approve, reject or mark paid somebody else's claim."""
    if action not in _ADMIN_ACTIONS:
        raise NotFoundError("Unknown action")
    claim = await _service.get_visible(db, user, claim_id)
    url = f"{ADMIN_URL}{claim.id}/"
    try:
        if action == "approve":
            await _service.approve(db, user, claim, request=request)
            notice = "approved"
        elif action == "reject":
            await _service.reject(db, user, claim, reason=reason, request=request)
            notice = "rejected"
        else:
            await _service.mark_paid(db, user, claim, request=request)
            notice = "paid"
    except AppError as exc:
        return _redirect(url, {"error": _error(exc)})
    return _redirect(url, {"notice": notice})


async def _csv(db: AsyncSession, viewer: User, claim: AiReimbursementClaim, request: Request) -> Response:
    body = await _service.export_csv(db, viewer, claim, request=request)
    filename = _service.export_filename(claim, await db.get(User, claim.user_id))
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(MY_URL + "{claim_id}/export/", response_model=None)
async def my_claim_export(
    claim_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    return await _csv(db, user, await _own(db, user, claim_id), request)


@router.get(ADMIN_URL + "{claim_id}/export/", response_model=None)
async def admin_claim_export(
    claim_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    return await _csv(db, user, await _service.get_visible(db, user, claim_id), request)
