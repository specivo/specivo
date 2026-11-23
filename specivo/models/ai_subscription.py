"""AI tools (a lookup), personal AI subscriptions (flat-rate seats people pay for) and company seat plans."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from specivo.core.money import Money
from specivo.models.base import Base, TimestampMixin
from specivo.models.money import amount_column, currency_column

_PROVIDER_CHECK = "~ '^[a-z0-9][a-z0-9_.-]{0,49}$'"


class AiTool(Base, TimestampMixin):
    """A tool that reports AI usage (Claude Code, Codex ...), keyed by slug.

    Seeded with the common coding agents. A slug a reporter sends that is not
    here yet is created on first use, inactive, so third-party adapters work
    without a server change; inactive tools are left out of pickers until an
    administrator activates them. Tools are never deleted.

    ``default_provider`` is the model provider usage from this tool reports
    when it names none, and the provider whose subscriptions cover it when
    they list no tools. Tools that route to several providers have none.
    """

    __tablename__ = "ai_tools"
    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{1,49}$'", name="ck_ai_tools_slug_format"),
        CheckConstraint(
            f"default_provider IS NULL OR default_provider {_PROVIDER_CHECK}",
            name="ck_ai_tools_default_provider_format",
        ),
        CheckConstraint("btrim(name) <> ''", name="ck_ai_tools_name_not_blank"),
    )

    slug: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    default_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    def __repr__(self) -> str:
        return f"<AiTool {self.slug}>"


class AiSubscription(Base, TimestampMixin):
    """One person's flat-rate AI plan (a personal seat), or a company seat plan, during a span of days.

    A **company seat plan** has no holder (``user_id`` ``NULL``) and a
    ``seat_count``; the people it covers are its members
    (:class:`AiSubscriptionMember`), each holding one seat for a span of days.
    Its fee may be zero (a plan whose cost is booked elsewhere); a personal
    plan's fee is above zero.

    ``starts_on`` and ``ends_on`` are UTC days, both inclusive; ``ends_on``
    ``NULL`` means the plan is still running (see
    :mod:`specivo.core.ai_subscriptions` for how they meet usage instants).
    ``tool_slugs`` lists the tools the plan covers; empty means every tool of
    ``provider``. The fee is ``amount`` in ``currency_code`` per
    ``billing_period`` (``month`` or ``year``), as the person typed it, so it
    is validated to the currency's minor units. ``billing_account_id`` links
    the plan to the billing account it is the fee schedule of.
    """

    __tablename__ = "ai_subscriptions"
    __table_args__ = (
        CheckConstraint("billing_period IN ('month', 'year')", name="ck_ai_subscriptions_billing_period"),
        CheckConstraint("amount > 0 OR (user_id IS NULL AND amount >= 0)", name="ck_ai_subscriptions_amount_positive"),
        CheckConstraint(
            "(user_id IS NULL) = (seat_count IS NOT NULL) AND (seat_count IS NULL OR seat_count >= 1)",
            name="ck_ai_subscriptions_company_seats",
        ),
        CheckConstraint("ends_on IS NULL OR ends_on >= starts_on", name="ck_ai_subscriptions_period"),
        CheckConstraint(f"provider {_PROVIDER_CHECK}", name="ck_ai_subscriptions_provider_format"),
        CheckConstraint("plan_key ~ '^[a-z0-9][a-z0-9-]{0,49}$'", name="ck_ai_subscriptions_plan_key_format"),
        CheckConstraint("btrim(plan_name) <> ''", name="ck_ai_subscriptions_plan_name_not_blank"),
        Index("ix_ai_subscriptions_user_starts", "user_id", "starts_on"),
        Index("ix_ai_subscriptions_currency_code", "currency_code"),
        Index("ix_ai_subscriptions_created_by_id", "created_by_id"),
        Index("ix_ai_subscriptions_billing_account_id", "billing_account_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: The holder; ``None`` for a company seat plan.
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    #: Seats of a company plan (its fee is split into this many equal shares); ``None`` for a personal plan.
    seat_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    plan_key: Mapped[str] = mapped_column(String(50), nullable=False)
    plan_name: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_slugs: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    amount: Mapped[Decimal] = amount_column("amount")
    currency_code: Mapped[str] = currency_column()
    billing_period: Mapped[str] = mapped_column(String(5), nullable=False)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: The billing account this plan is the fee schedule of (a personal
    #: ``subscription`` account, created with the subscription).
    billing_account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ai_billing_accounts.id", ondelete="SET NULL"), nullable=True
    )

    @property
    def fee(self) -> Money:
        return Money(self.amount, self.currency_code)

    @property
    def is_company(self) -> bool:
        """Whether this is a company seat plan (no holder)."""
        return self.user_id is None

    def __repr__(self) -> str:
        return f"<AiSubscription {self.id} user={self.user_id} {self.plan_key} {self.starts_on}..{self.ends_on}>"


class AiSubscriptionMember(Base, TimestampMixin):
    """One person's seat on a company seat plan during a span of UTC days (both inclusive).

    Memberships are dated rather than replaced, so the months before a
    change keep their allocation: removing someone sets ``ends_on``. One
    person's memberships of one plan never overlap, and on no day do more
    people hold a seat than the plan has (both checked by the service).
    """

    __tablename__ = "ai_subscription_members"
    __table_args__ = (
        CheckConstraint("ends_on IS NULL OR ends_on >= starts_on", name="ck_ai_subscription_members_period"),
        Index("ix_ai_subscription_members_subscription", "subscription_id", "starts_on"),
        Index("ix_ai_subscription_members_user", "user_id", "starts_on"),
        Index("ix_ai_subscription_members_created_by_id", "created_by_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ai_subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"<AiSubscriptionMember {self.id} plan={self.subscription_id} user={self.user_id} "
            f"{self.starts_on}..{self.ends_on}>"
        )
