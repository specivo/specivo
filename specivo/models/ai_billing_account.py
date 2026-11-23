"""AI billing accounts: what AI usage was paid through, and who pays (see :mod:`specivo.core.ai_billing`)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from specivo.core.ai_billing import (
    ACCOUNT_DEFAULT_COST_TYPES,
    ACCOUNT_PAYERS,
    BILLING_ACCOUNT_KINDS,
    PAYER_EMPLOYEE,
)
from specivo.models.base import Base, TimestampMixin


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class AiBillingAccount(Base, TimestampMixin):
    """A corporate gateway, a company plan, a personal subscription or key, or a local model.

    ``owner_user_id`` ``NULL`` is an instance (company) account, managed by
    administrators; otherwise it is that person's own account, which they
    manage while AI subscription self-service is on. ``slug`` is unique per
    owner (and among instance accounts).

    ``payer`` is who carries the cost. ``reimburse_share`` (0 to 1) is the part
    of an employee-paid account's spend the employer reimburses; employer
    accounts have none. ``route_patterns`` are ``fnmatch`` patterns over an
    entry's route; ``tool_slugs`` the tools the account covers (empty: every
    tool). ``default_cost_type`` is the cost type of usage through the
    account when the record gives none. Accounts are deactivated, not deleted,
    once usage points at them.

    A personal subscription is the fee schedule of an account of kind
    ``subscription`` (``ai_subscriptions.billing_account_id``); its coverage
    stays the subscription's, so that account lists no tools.
    """

    __tablename__ = "ai_billing_accounts"
    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'", name="ck_ai_billing_accounts_slug_format"),
        CheckConstraint("btrim(name) <> ''", name="ck_ai_billing_accounts_name_not_blank"),
        CheckConstraint(_in("kind", BILLING_ACCOUNT_KINDS), name="ck_ai_billing_accounts_kind"),
        CheckConstraint(_in("payer", ACCOUNT_PAYERS), name="ck_ai_billing_accounts_payer"),
        CheckConstraint(
            f"reimburse_share IS NULL OR (payer = '{PAYER_EMPLOYEE}'"
            " AND reimburse_share >= 0 AND reimburse_share <= 1)",
            name="ck_ai_billing_accounts_reimburse_share",
        ),
        CheckConstraint(
            f"default_cost_type IS NULL OR {_in('default_cost_type', ACCOUNT_DEFAULT_COST_TYPES)}",
            name="ck_ai_billing_accounts_default_cost_type",
        ),
        Index(
            "uq_ai_billing_accounts_owner_slug",
            "owner_user_id",
            "slug",
            unique=True,
            postgresql_where=text("owner_user_id IS NOT NULL"),
        ),
        Index(
            "uq_ai_billing_accounts_instance_slug",
            "slug",
            unique=True,
            postgresql_where=text("owner_user_id IS NULL"),
        ),
        Index("ix_ai_billing_accounts_created_by_id", "created_by_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    owner_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    payer: Mapped[str] = mapped_column(String(8), nullable=False)
    reimburse_share: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    route_patterns: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    tool_slugs: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    default_cost_type: Mapped[str | None] = mapped_column(String(12), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    @property
    def is_instance(self) -> bool:
        """Whether this is a company account (no owner)."""
        return self.owner_user_id is None

    def __repr__(self) -> str:
        owner = "instance" if self.owner_user_id is None else f"user={self.owner_user_id}"
        return f"<AiBillingAccount {self.id} {self.slug} {owner} {self.payer}>"


def subscription_account_slug(plan_key: str, subscription_id: int) -> str:
    """Slug of the account a subscription is the fee schedule of (the migration back-fill uses the same)."""
    return f"{plan_key}-{subscription_id}"


def subscription_account(
    *, subscription_id: int, user_id: int, plan_key: str, plan_name: str, created_by_id: int | None
) -> AiBillingAccount:
    """A new personal account for a subscription: kind ``subscription``, paid by the employee, no tools.

    The subscription keeps its own coverage (tools or provider), so the
    account lists no tools of its own.
    """
    return AiBillingAccount(
        slug=subscription_account_slug(plan_key, subscription_id),
        name=plan_name,
        kind="subscription",
        owner_user_id=user_id,
        payer=PAYER_EMPLOYEE,
        reimburse_share=None,
        route_patterns=[],
        tool_slugs=[],
        default_cost_type="subscription",
        is_active=True,
        created_by_id=created_by_id,
    )
