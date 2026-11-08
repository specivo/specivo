"""AI tool and AI subscription schemas.

A fee crosses the API as a **string** (``"20.00"``), never a JSON number, like
every amount (see :mod:`specivo.schemas.money`). ``starts_on`` and ``ends_on``
are UTC days, both inclusive.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from specivo.core.ai_subscriptions import PlanPreset
from specivo.core.money import format_decimal_plain
from specivo.models.ai_subscription import AiSubscription, AiTool
from specivo.models.user import User
from specivo.schemas.money import CurrencyCode


class AiToolOut(BaseModel):
    slug: str
    name: str
    default_provider: str | None
    is_active: bool
    position: int

    @classmethod
    def from_row(cls, row: AiTool) -> AiToolOut:
        return cls(
            slug=row.slug,
            name=row.name,
            default_provider=row.default_provider,
            is_active=row.is_active,
            position=row.position,
        )


class AiToolCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(max_length=50, examples=["aider"])
    name: str = Field(max_length=100, examples=["Aider"])
    default_provider: str | None = Field(None, max_length=50, examples=["anthropic"])
    is_active: bool = True
    position: int = Field(0, ge=-100000, le=100000)


class AiToolUpdate(BaseModel):
    """Leave a field out to keep it; ``default_provider: null`` clears it."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, max_length=100)
    default_provider: str | None = Field(None, max_length=50)
    is_active: bool | None = None
    position: int | None = Field(None, ge=-100000, le=100000)


class AiPlanPresetOut(BaseModel):
    """A known plan with a suggested list price (a hint that may be out of date)."""

    key: str
    name: str
    provider: str
    amount: str = Field(examples=["20"])
    currency: str
    billing_period: str
    tool_slugs: list[str]

    @classmethod
    def from_preset(cls, preset: PlanPreset) -> AiPlanPresetOut:
        return cls(
            key=preset.key,
            name=preset.name,
            provider=preset.provider,
            amount=format_decimal_plain(preset.money.quantize().amount),
            currency=preset.currency,
            billing_period=preset.billing_period,
            tool_slugs=list(preset.tool_slugs),
        )


class AiSubscriptionCreate(BaseModel):
    """A new subscription.

    With a preset ``plan_key`` the fields left out take the preset's values
    (provider, name, tools, suggested price and currency, monthly billing).
    With ``custom`` give ``provider``, ``plan_name``, ``amount`` and
    ``currency``. ``tool_slugs`` empty means every tool of the provider.
    """

    model_config = ConfigDict(extra="forbid")

    plan_key: str = Field("custom", max_length=50, examples=["claude-max-5x"])
    provider: str | None = Field(None, max_length=50, examples=["anthropic"])
    plan_name: str | None = Field(None, max_length=100)
    tool_slugs: list[str] | None = Field(None, max_length=20)
    amount: StrictStr | None = Field(None, examples=["100.00"])
    currency: CurrencyCode | None = None
    billing_period: str | None = Field(None, examples=["month", "year"])
    starts_on: date
    ends_on: date | None = None
    notes: str | None = Field(None, max_length=2000)


class AiSubscriptionAdminCreate(AiSubscriptionCreate):
    user_id: int = Field(description="The person who holds the subscription")


class AiSubscriptionUpdate(BaseModel):
    """Leave a field out to keep it; ``ends_on: null`` makes the subscription open-ended."""

    model_config = ConfigDict(extra="forbid")

    plan_key: str | None = Field(None, max_length=50)
    provider: str | None = Field(None, max_length=50)
    plan_name: str | None = Field(None, max_length=100)
    tool_slugs: list[str] | None = Field(None, max_length=20)
    amount: StrictStr | None = None
    currency: CurrencyCode | None = None
    billing_period: str | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    notes: str | None = Field(None, max_length=2000)

    def changes(self) -> dict[str, object]:
        """The fields the client sent (``None`` values only where a field may be cleared)."""
        sent = self.model_fields_set
        out: dict[str, object] = {}
        for name in sent:
            value = getattr(self, name)
            if value is None and name not in ("ends_on", "notes"):
                continue
            out[name] = value
        return out


class AiSubscriptionOut(BaseModel):
    id: int
    user_id: int
    user_login: str | None = None
    provider: str
    plan_key: str
    plan_name: str
    tool_slugs: list[str]
    amount: str = Field(examples=["20.00"], description="The fee per billing period, at the currency's minor units")
    currency: str
    billing_period: str
    starts_on: date
    ends_on: date | None
    notes: str | None
    created_by_id: int | None
    created_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def from_row(cls, row: AiSubscription, owner: User | None = None) -> AiSubscriptionOut:
        return cls(
            id=row.id,
            user_id=row.user_id,
            user_login=owner.login if owner is not None else None,
            provider=row.provider,
            plan_key=row.plan_key,
            plan_name=row.plan_name,
            tool_slugs=list(row.tool_slugs or []),
            amount=format_decimal_plain(row.fee.quantize().amount),
            currency=row.currency_code,
            billing_period=row.billing_period,
            starts_on=row.starts_on,
            ends_on=row.ends_on,
            notes=row.notes,
            created_by_id=row.created_by_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class AiSubscriptionSettingsOut(BaseModel):
    self_service: bool = Field(description="Whether people may add and change their own subscriptions")


class AiSubscriptionSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    self_service: bool
