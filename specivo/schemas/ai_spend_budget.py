"""AI spend budget schemas.

Amounts cross the API as **strings** (``"500"``, ``"412.30"``), never JSON
numbers, like every money value (see :mod:`specivo.schemas.money`).
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from specivo.core.money import format_decimal_plain
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.ai_spend_budget import AiSpendBudget
from specivo.services.ai_spend_budget_service import TARGET_ACCOUNT, TARGET_PAYER, BudgetProgress


class AiSpendBudgetCreate(BaseModel):
    """A monthly budget on one of your own billing accounts.

    ``thresholds`` are the percents of the amount that send an alert, once
    each per month (default 80 and 100; at most 5, from 1 to 1000).
    """

    model_config = ConfigDict(extra="forbid")

    billing_account_id: int = Field(description="The billing account the budget covers")
    amount: StrictStr = Field(examples=["500"], description="The monthly budget, a decimal string")
    currency: StrictStr = Field(min_length=3, max_length=3, examples=["USD"])
    thresholds: list[int] | None = Field(None, max_length=5, examples=[[80, 100]])
    is_active: bool = True


class AiSpendBudgetAdminCreate(BaseModel):
    """A monthly budget on any billing account, or on everything one payer pays (``employer``, ``employee``)."""

    model_config = ConfigDict(extra="forbid")

    billing_account_id: int | None = Field(None, description="The billing account; leave out for a payer budget")
    payer: str | None = Field(None, examples=["employer"], description="employer or employee; or a billing account")
    amount: StrictStr = Field(examples=["5000"])
    currency: StrictStr = Field(min_length=3, max_length=3, examples=["USD"])
    thresholds: list[int] | None = Field(None, max_length=5, examples=[[80, 100]])
    is_active: bool = True


class AiSpendBudgetUpdate(BaseModel):
    """Leave a field out to keep it. What a budget covers never changes: remove it and set another."""

    model_config = ConfigDict(extra="forbid")

    amount: StrictStr | None = None
    currency: StrictStr | None = Field(None, min_length=3, max_length=3)
    thresholds: list[int] | None = Field(None, max_length=5)
    is_active: bool | None = None

    def changes(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.model_fields_set if getattr(self, name) is not None}


class AiSpendBudgetOut(BaseModel):
    id: int
    target: str = Field(description="billing_account or payer")
    billing_account_id: int | None
    billing_account_slug: str | None = None
    billing_account_name: str | None = None
    owner_user_id: int | None = Field(None, description="The owner of a personal account; null otherwise")
    payer: str | None = Field(None, description="For a payer budget: employer or employee")
    amount: str = Field(examples=["500"])
    currency: str
    thresholds: list[int]
    is_active: bool
    period_start: date | None = Field(None, description="The month measured (its first day, UTC)")
    as_of: date | None = Field(None, description="The last day counted")
    spend: str | None = Field(None, examples=["412.30"], description="Spend so far this month, in the currency")
    percent: str | None = Field(None, examples=["82.5"])
    reached: list[int] = Field(default_factory=list, description="Thresholds the spend has reached")
    alerted: list[int] = Field(default_factory=list, description="Thresholds an alert went out for this month")
    unconverted: bool = Field(False, description="Some spend has no exchange rate into the currency and is left out")
    created_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def from_row(
        cls, row: AiSpendBudget, account: AiBillingAccount | None, progress: BudgetProgress | None
    ) -> AiSpendBudgetOut:
        return cls(
            id=row.id,
            target=TARGET_ACCOUNT if row.billing_account_id is not None else TARGET_PAYER,
            billing_account_id=row.billing_account_id,
            billing_account_slug=account.slug if account is not None else None,
            billing_account_name=account.name if account is not None else None,
            owner_user_id=account.owner_user_id if account is not None else None,
            payer=row.payer,
            amount=format_decimal_plain(row.amount_amount.normalize()),
            currency=row.amount_currency,
            thresholds=list(row.thresholds),
            is_active=row.is_active,
            period_start=progress.period_start if progress else None,
            as_of=progress.as_of if progress else None,
            spend=progress.spend.quantize().amount_str if progress else None,
            percent=format_decimal_plain(progress.percent) if progress else None,
            reached=list(progress.reached) if progress else [],
            alerted=list(progress.alerted) if progress else [],
            unconverted=progress.unconverted if progress else False,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
