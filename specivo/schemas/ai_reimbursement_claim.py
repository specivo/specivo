"""AI reimbursement claim schemas.

Amounts cross the API as strings (:class:`specivo.schemas.money.MoneyOut`), the
share as a string such as ``"0.5"``. A claim's month is ``YYYY-MM``.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from specivo.core.money import Money
from specivo.models.ai_reimbursement_claim import AiReimbursementClaim, AiReimbursementClaimEntry
from specivo.models.user import User
from specivo.schemas.ai_billing_account import share_text
from specivo.schemas.money import MoneyOut
from specivo.services.ai_reimbursement_claim_service import NOTE_MAX_LENGTH, ClaimChange, ClaimReferences, month_text


class AiReimbursementClaimCreate(BaseModel):
    """A new claim for a month that has ended.

    ``currency`` defaults to the instance reporting currency; it must be enabled.
    """

    model_config = ConfigDict(extra="forbid")

    month: str = Field(examples=["2026-03"], description="YYYY-MM")
    currency: str | None = Field(None, examples=["USD"])
    note: str | None = Field(None, max_length=NOTE_MAX_LENGTH)


class AiReimbursementClaimRefresh(BaseModel):
    """Refresh a draft; ``note`` (when given) replaces the note, ``""`` clears it."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(None, max_length=NOTE_MAX_LENGTH)


class AiReimbursementClaimReject(BaseModel):
    """Reject a submitted claim, with a reason the holder reads (optional, never audited)."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(None, max_length=NOTE_MAX_LENGTH)


class ClaimPersonOut(BaseModel):
    id: int
    login: str | None
    name: str | None

    @classmethod
    def of(cls, user_id: int | None, users: dict[int, User]) -> ClaimPersonOut | None:
        if user_id is None:
            return None
        user = users.get(user_id)
        return cls(id=user_id, login=user.login if user else None, name=user.display_name if user else None)


class AiReimbursementClaimEntryOut(BaseModel):
    """One frozen entry. ``changed`` is why the entry is no longer what the claim froze, or null."""

    id: int
    entry_id: int | None
    revision: int
    usage_date: date
    project_key: str | None
    billing_account: str | None
    tool: str
    model: str
    payer: str
    cost_type: str
    share: str
    marginal: MoneyOut
    seat_cost: MoneyOut
    reimbursable: MoneyOut
    changed: str | None = Field(None, description="deleted, excluded, billing or share")

    @classmethod
    def from_row(
        cls, row: AiReimbursementClaimEntry, refs: ClaimReferences, changed: str | None
    ) -> AiReimbursementClaimEntryOut:
        return cls(
            id=row.id,
            entry_id=row.entry_id,
            revision=row.revision,
            usage_date=row.usage_date,
            project_key=refs.projects.get(row.project_id) if row.project_id is not None else None,
            billing_account=refs.accounts.get(row.billing_account_id) if row.billing_account_id is not None else None,
            tool=row.tool_slug,
            model=row.model,
            payer=row.payer,
            cost_type=row.cost_type,
            share=share_text(row.share) or "0",
            marginal=MoneyOut.from_money(Money(row.marginal_amount, row.currency)),
            seat_cost=MoneyOut.from_money(Money(row.seat_amount, row.currency)),
            reimbursable=MoneyOut.from_money(Money(row.reimbursable_amount, row.currency)),
            changed=changed,
        )


class AiReimbursementClaimOut(BaseModel):
    """A claim. ``changed_entries`` counts the entries edited, re-priced or removed since the snapshot."""

    id: int
    holder: ClaimPersonOut
    month: str = Field(examples=["2026-03"])
    status: str
    total: MoneyOut
    entry_count: int
    note: str | None
    computed_at: datetime
    submitted_at: datetime | None
    decided_at: datetime | None
    decided_by: ClaimPersonOut | None
    decision_note: str | None
    paid_at: datetime | None
    paid_by: ClaimPersonOut | None
    changed_entries: int

    @classmethod
    def from_row(
        cls, claim: AiReimbursementClaim, refs: ClaimReferences, changes: list[ClaimChange]
    ) -> AiReimbursementClaimOut:
        holder = ClaimPersonOut.of(claim.user_id, refs.users) or ClaimPersonOut(id=claim.user_id, login=None, name=None)
        return cls(
            id=claim.id,
            holder=holder,
            month=month_text(claim.month),
            status=claim.status,
            total=MoneyOut.from_money(claim.total),
            entry_count=claim.entry_count,
            note=claim.note,
            computed_at=claim.computed_at,
            submitted_at=claim.submitted_at,
            decided_at=claim.decided_at,
            decided_by=ClaimPersonOut.of(claim.decided_by_id, refs.users),
            decision_note=claim.decision_note,
            paid_at=claim.paid_at,
            paid_by=ClaimPersonOut.of(claim.paid_by_id, refs.users),
            changed_entries=len(changes),
        )


class AiReimbursementClaimDetailOut(AiReimbursementClaimOut):
    """A claim with its frozen entries."""

    entries: list[AiReimbursementClaimEntryOut]
