"""AI reimbursement claims API.

- ``/my/ai-reimbursement-claims/``: the caller's own claims. Create one for a
  month that has ended (a draft, priced from the month's reimbursable usage),
  refresh a draft, submit it, withdraw a submitted one, delete a draft or a
  rejected one, export one as CSV.
- ``/admin/ai-reimbursement-claims/``: administrators list everyone's claims,
  approve or reject a submitted claim and mark an approved one paid (never
  their own), and export any claim.

Claims are account-wide, so a project-limited API key is refused. The rules
are :class:`AiReimbursementClaimService`'s, including the snapshot rule: a
submitted claim's amounts never change; ``changed_entries`` and each entry's
``changed`` say what was edited, re-priced or removed since.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import DbSession
from specivo.core.exceptions import NotFoundError
from specivo.core.security import get_account_user
from specivo.models.ai_reimbursement_claim import AiReimbursementClaim
from specivo.models.user import User
from specivo.schemas.ai_reimbursement_claim import (
    AiReimbursementClaimCreate,
    AiReimbursementClaimDetailOut,
    AiReimbursementClaimEntryOut,
    AiReimbursementClaimOut,
    AiReimbursementClaimRefresh,
    AiReimbursementClaimReject,
)
from specivo.schemas.common import PaginatedResponse
from specivo.services.ai_reimbursement_claim_service import AiReimbursementClaimService

router = APIRouter(tags=["ai-reimbursement-claims"])
_service = AiReimbursementClaimService()


async def claims_out(db: AsyncSession, claims: list[AiReimbursementClaim]) -> list[AiReimbursementClaimOut]:
    refs = await _service.references(db, claims, [])
    changes = await _service.changes(db, (c.id for c in claims))
    return [AiReimbursementClaimOut.from_row(c, refs, changes.get(c.id, [])) for c in claims]


async def claim_detail(db: AsyncSession, claim: AiReimbursementClaim) -> AiReimbursementClaimDetailOut:
    await db.refresh(claim)
    entries = await _service.entries(db, claim)
    refs = await _service.references(db, [claim], entries)
    changes = (await _service.changes(db, [claim.id])).get(claim.id, [])
    reasons = {c.claim_entry_id: c.reason for c in changes}
    base = AiReimbursementClaimOut.from_row(claim, refs, changes)
    return AiReimbursementClaimDetailOut(
        **base.model_dump(),
        entries=[AiReimbursementClaimEntryOut.from_row(e, refs, reasons.get(e.id)) for e in entries],
    )


async def _own(db: AsyncSession, user: User, claim_id: int) -> AiReimbursementClaim:
    """*user*'s own claim; 404 for anybody else's (administrators included: they use the admin routes)."""
    claim = await _service.get_visible(db, user, claim_id)
    if claim.user_id != user.id:
        raise NotFoundError("AI reimbursement claim not found")
    return claim


async def _csv(db: AsyncSession, viewer: User, claim: AiReimbursementClaim, request: Request) -> Response:
    body = await _service.export_csv(db, viewer, claim, request=request)
    holder = await db.get(User, claim.user_id)
    filename = _service.export_filename(claim, holder)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# My claims
# ---------------------------------------------------------------------------


@router.get("/my/ai-reimbursement-claims/", response_model=list[AiReimbursementClaimOut])
async def list_my_claims(
    db: DbSession, current_user: User = Depends(get_account_user)
) -> list[AiReimbursementClaimOut]:
    """The caller's claims, newest month first."""
    return await claims_out(db, await _service.list_own(db, current_user))


@router.post("/my/ai-reimbursement-claims/", response_model=AiReimbursementClaimDetailOut, status_code=201)
async def create_my_claim(
    data: AiReimbursementClaimCreate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(get_account_user),
) -> AiReimbursementClaimDetailOut:
    """A draft claim for a month that has ended.

    422 ``ai_reimbursement_claim_invalid`` (bad or running month),
    ``ai_reimbursement_claim_empty`` (nothing to reimburse) or
    ``ai_reimbursement_claim_unconverted`` (no exchange rate); 409
    ``ai_reimbursement_claim_exists``; 403 for a service account.
    """
    claim = await _service.create(db, current_user, data.month, currency=data.currency, note=data.note)
    return await claim_detail(db, claim)


@router.get("/my/ai-reimbursement-claims/{claim_id}/", response_model=AiReimbursementClaimDetailOut)
async def get_my_claim(
    claim_id: int, db: DbSession, current_user: User = Depends(get_account_user)
) -> AiReimbursementClaimDetailOut:
    return await claim_detail(db, await _own(db, current_user, claim_id))


@router.post("/my/ai-reimbursement-claims/{claim_id}/refresh/", response_model=AiReimbursementClaimDetailOut)
async def refresh_my_claim(
    claim_id: int,
    db: DbSession,
    data: AiReimbursementClaimRefresh | None = None,
    current_user: User = Depends(get_account_user),
) -> AiReimbursementClaimDetailOut:
    """Take the month's current figures into a draft (409 once submitted)."""
    claim = await _own(db, current_user, claim_id)
    claim = await _service.refresh(db, current_user, claim, note=data.note if data is not None else None)
    return await claim_detail(db, claim)


@router.post("/my/ai-reimbursement-claims/{claim_id}/submit/", response_model=AiReimbursementClaimDetailOut)
async def submit_my_claim(
    claim_id: int, request: Request, db: DbSession, current_user: User = Depends(get_account_user)
) -> AiReimbursementClaimDetailOut:
    """draft -> submitted; the amounts are frozen from now on."""
    claim = await _own(db, current_user, claim_id)
    return await claim_detail(db, await _service.submit(db, current_user, claim, request=request))


@router.post("/my/ai-reimbursement-claims/{claim_id}/withdraw/", response_model=AiReimbursementClaimDetailOut)
async def withdraw_my_claim(
    claim_id: int, request: Request, db: DbSession, current_user: User = Depends(get_account_user)
) -> AiReimbursementClaimDetailOut:
    """submitted -> draft."""
    claim = await _own(db, current_user, claim_id)
    return await claim_detail(db, await _service.withdraw(db, current_user, claim, request=request))


@router.delete("/my/ai-reimbursement-claims/{claim_id}/", status_code=204)
async def delete_my_claim(claim_id: int, db: DbSession, current_user: User = Depends(get_account_user)) -> Response:
    """Delete a draft or rejected claim (409 for any other)."""
    claim = await _own(db, current_user, claim_id)
    await _service.delete(db, current_user, claim)
    return Response(status_code=204)


@router.get("/my/ai-reimbursement-claims/{claim_id}/export/", response_class=Response)
async def export_my_claim(
    claim_id: int, request: Request, db: DbSession, current_user: User = Depends(get_account_user)
) -> Response:
    """The claim as CSV (audited)."""
    return await _csv(db, current_user, await _own(db, current_user, claim_id), request)


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------


@router.get("/admin/ai-reimbursement-claims/", response_model=PaginatedResponse[AiReimbursementClaimOut])
async def admin_list_claims(
    db: DbSession,
    current_user: User = Depends(require_admin_api),
    status: str | None = Query(None, description="draft, submitted, approved, paid or rejected"),
    user_id: int | None = Query(None, description="One person's claims"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> PaginatedResponse[AiReimbursementClaimOut]:
    """Everyone's claims, the oldest submission first (admin only)."""
    rows, total = await _service.list_claims(
        db, current_user, status=status, user_id=user_id, offset=offset, limit=limit
    )
    return PaginatedResponse[AiReimbursementClaimOut](
        total_count=total, offset=offset, limit=limit, items=await claims_out(db, rows)
    )


@router.get("/admin/ai-reimbursement-claims/{claim_id}/", response_model=AiReimbursementClaimDetailOut)
async def admin_get_claim(
    claim_id: int, db: DbSession, current_user: User = Depends(require_admin_api)
) -> AiReimbursementClaimDetailOut:
    return await claim_detail(db, await _service.get_visible(db, current_user, claim_id))


@router.post("/admin/ai-reimbursement-claims/{claim_id}/approve/", response_model=AiReimbursementClaimDetailOut)
async def admin_approve_claim(
    claim_id: int, request: Request, db: DbSession, current_user: User = Depends(require_admin_api)
) -> AiReimbursementClaimDetailOut:
    """submitted -> approved, at the frozen amount (not your own claim)."""
    claim = await _service.get_visible(db, current_user, claim_id)
    return await claim_detail(db, await _service.approve(db, current_user, claim, request=request))


@router.post("/admin/ai-reimbursement-claims/{claim_id}/reject/", response_model=AiReimbursementClaimDetailOut)
async def admin_reject_claim(
    claim_id: int,
    request: Request,
    db: DbSession,
    data: AiReimbursementClaimReject | None = None,
    current_user: User = Depends(require_admin_api),
) -> AiReimbursementClaimDetailOut:
    """submitted -> rejected, with an optional reason the holder reads (not your own claim)."""
    claim = await _service.get_visible(db, current_user, claim_id)
    reason = data.reason if data is not None else None
    return await claim_detail(db, await _service.reject(db, current_user, claim, reason=reason, request=request))


@router.post("/admin/ai-reimbursement-claims/{claim_id}/paid/", response_model=AiReimbursementClaimDetailOut)
async def admin_mark_claim_paid(
    claim_id: int, request: Request, db: DbSession, current_user: User = Depends(require_admin_api)
) -> AiReimbursementClaimDetailOut:
    """approved -> paid (not your own claim)."""
    claim = await _service.get_visible(db, current_user, claim_id)
    return await claim_detail(db, await _service.mark_paid(db, current_user, claim, request=request))


@router.get("/admin/ai-reimbursement-claims/{claim_id}/export/", response_class=Response)
async def admin_export_claim(
    claim_id: int, request: Request, db: DbSession, current_user: User = Depends(require_admin_api)
) -> Response:
    """Any claim as CSV (audited)."""
    return await _csv(db, current_user, await _service.get_visible(db, current_user, claim_id), request)
