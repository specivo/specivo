"""AI billing account schemas.

A reimbursement share crosses the API as a **string** (``"0.5"``), never a
JSON number, like every decimal amount (see :mod:`specivo.schemas.money`).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.user import User


def share_text(value: object) -> str | None:
    """A stored share as the API shows it: ``"0.5"``, ``"1"``; ``None`` when there is none."""
    if value is None:
        return None
    return format(Decimal(str(value)).normalize(), "f")


class AiBillingAccountCreate(BaseModel):
    """A new billing account.

    ``kind`` is ``gateway``, ``subscription``, ``api_key`` or ``local``;
    ``payer`` is ``employer`` or ``employee``. ``reimburse_share`` (0 to 1,
    employee-paid accounts only) is the part of the spend the employer pays
    back. ``route_patterns`` are case-insensitive ``fnmatch`` patterns over the
    route a reporter sends (for example ``corp-*``); ``tool_slugs`` empty
    covers every tool. ``default_cost_type`` is used when a record gives none.
    """

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(max_length=64, examples=["corp-gateway"])
    name: str = Field(max_length=100, examples=["Company LiteLLM gateway"])
    kind: str = Field(examples=["gateway"])
    payer: str = Field(examples=["employer"])
    reimburse_share: StrictStr | None = Field(None, examples=["0.5"])
    route_patterns: list[str] = Field(default_factory=list, max_length=20, examples=[["corp-*"]])
    tool_slugs: list[str] = Field(default_factory=list, max_length=20)
    default_cost_type: str | None = Field(None, examples=["api"])
    is_active: bool = True

    def changes(self) -> dict[str, object]:
        return self.model_dump()


class AiBillingAccountAdminCreate(AiBillingAccountCreate):
    owner_user_id: int | None = Field(
        None, description="The person who owns a personal account; leave out for an instance (company) account"
    )

    def changes(self) -> dict[str, object]:
        return self.model_dump(exclude={"owner_user_id"})


class AiBillingAccountUpdate(BaseModel):
    """Leave a field out to keep it; ``reimburse_share`` / ``default_cost_type`` ``null`` clear them."""

    model_config = ConfigDict(extra="forbid")

    slug: str | None = Field(None, max_length=64)
    name: str | None = Field(None, max_length=100)
    kind: str | None = None
    payer: str | None = None
    reimburse_share: StrictStr | None = None
    route_patterns: list[str] | None = Field(None, max_length=20)
    tool_slugs: list[str] | None = Field(None, max_length=20)
    default_cost_type: str | None = None
    is_active: bool | None = None

    def changes(self) -> dict[str, object]:
        """The fields the client sent (``None`` only where a field may be cleared)."""
        out: dict[str, object] = {}
        for name in self.model_fields_set:
            value = getattr(self, name)
            if value is None and name not in ("reimburse_share", "default_cost_type"):
                continue
            out[name] = value
        return out


class AiBillingAccountOut(BaseModel):
    id: int
    slug: str
    name: str
    kind: str
    is_instance: bool = Field(description="A company account (no owner), managed by administrators")
    owner_user_id: int | None
    owner_login: str | None = None
    payer: str
    reimburse_share: str | None = Field(examples=["0.5"])
    route_patterns: list[str]
    tool_slugs: list[str]
    default_cost_type: str | None
    is_active: bool
    subscription_ids: list[int] = Field(
        default_factory=list, description="The subscriptions this account is the fee schedule of"
    )
    created_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def from_row(
        cls, row: AiBillingAccount, owner: User | None = None, subscription_ids: list[int] | None = None
    ) -> AiBillingAccountOut:
        return cls(
            id=row.id,
            slug=row.slug,
            name=row.name,
            kind=row.kind,
            is_instance=row.owner_user_id is None,
            owner_user_id=row.owner_user_id,
            owner_login=owner.login if owner is not None else None,
            payer=row.payer,
            reimburse_share=share_text(row.reimburse_share),
            route_patterns=list(row.route_patterns or []),
            tool_slugs=list(row.tool_slugs or []),
            default_cost_type=row.default_cost_type,
            is_active=row.is_active,
            subscription_ids=list(subscription_ids or []),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
