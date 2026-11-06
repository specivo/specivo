"""AI usage entries: tokens a person's AI tools used, a sibling of ``TimeEntry``.

One entry is one bucket of usage: one session x one model x one UTC day. A
reporter updates its bucket in place by sending the full totals again
(``idempotency_key``, scoped per target user). People log entries by hand too.

Two people are named on every entry: ``user_id``, in whose name the usage is
logged (on behalf of), and ``reporter_user_id`` / ``reporter_api_key_id``, the
account and key that wrote it. The reporter columns, ``source`` and the client
name and version never change after the insert.

Costs are a **snapshot at write time** (see ``AiUsageService``):
``price_id`` and ``priced_at`` record which price row was used and at which
instant; editing or adding prices never changes an entry by itself. Only an
administrator's "recompute entries in range" re-prices stored entries, each
at its own ``priced_at``.

No prompt, code, file path or tool content is ever stored.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from specivo.core.ai_pricing import TOKEN_CLASSES
from specivo.core.money import Money
from specivo.models.base import Base, TimestampMixin
from specivo.models.money import amount_column, currency_column, money_or_none, money_pair_constraints

#: How an entry was written. Reporter and import entries carry an idempotency
#: key and their token, model and tool columns are locked.
AI_USAGE_SOURCES: tuple[str, ...] = ("manual", "api", "mcp", "reporter", "import")
LOCKED_SOURCES: frozenset[str] = frozenset({"reporter", "import"})

#: How the entry's project and issue were found.
AI_USAGE_ATTRIBUTION_METHODS: tuple[str, ...] = ("explicit", "env", "mcp_session", "commit", "branch", "repo", "none")

#: Who pays for the usage, which decides the marginal cost.
AI_USAGE_COST_TYPES: tuple[str, ...] = ("subscription", "api", "local", "free", "unknown")

#: How the costs were obtained.
AI_USAGE_COST_BASES: tuple[str, ...] = ("calculated", "reported", "manual", "unpriced", "partial")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class AiUsageEntry(Base, TimestampMixin):
    """One bucket of AI usage (session x model x UTC day), logged in a user's name."""

    __tablename__ = "ai_usage_entries"

    __table_args__ = (
        CheckConstraint(_in("source", AI_USAGE_SOURCES), name="ck_ai_usage_entries_source"),
        CheckConstraint(
            _in("attribution_method", AI_USAGE_ATTRIBUTION_METHODS), name="ck_ai_usage_entries_attribution_method"
        ),
        CheckConstraint(_in("cost_type", AI_USAGE_COST_TYPES), name="ck_ai_usage_entries_cost_type"),
        CheckConstraint(_in("cost_basis", AI_USAGE_COST_BASES), name="ck_ai_usage_entries_cost_basis"),
        CheckConstraint(
            "source NOT IN ('reporter', 'import') OR idempotency_key IS NOT NULL",
            name="ck_ai_usage_entries_key_required",
        ),
        CheckConstraint("issue_id IS NULL OR project_id IS NOT NULL", name="ck_ai_usage_entries_issue_needs_project"),
        CheckConstraint(
            " AND ".join(f"{c} >= 0" for c in TOKEN_CLASSES) + " AND request_count >= 0",
            name="ck_ai_usage_entries_counts_non_negative",
        ),
        CheckConstraint(
            "ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at",
            name="ck_ai_usage_entries_period",
        ),
        CheckConstraint(
            "subscription_id IS NULL OR cost_type = 'subscription'",
            name="ck_ai_usage_entries_subscription_cost_type",
        ),
        CheckConstraint("revision >= 1", name="ck_ai_usage_entries_revision"),
        CheckConstraint(
            "(marginal_amount IS NULL OR marginal_amount >= 0)"
            " AND (api_equivalent_amount IS NULL OR api_equivalent_amount >= 0)"
            " AND (reported_cost_amount IS NULL OR reported_cost_amount >= 0)",
            name="ck_ai_usage_entries_amounts_non_negative",
        ),
        *money_pair_constraints("ai_usage_entries", "marginal", nullable=True),
        *money_pair_constraints("ai_usage_entries", "api_equivalent", nullable=True),
        *money_pair_constraints("ai_usage_entries", "reported_cost", nullable=True),
        Index(
            "uq_ai_usage_entries_user_key",
            "user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_ai_usage_entries_project_date", "project_id", "usage_date"),
        Index("ix_ai_usage_entries_user_date", "user_id", "usage_date"),
        Index("ix_ai_usage_entries_issue_id", "issue_id"),
        Index("ix_ai_usage_entries_reporter_date", "reporter_user_id", "usage_date"),
        Index("ix_ai_usage_entries_model_priced_at", "provider", "model", "priced_at"),
        Index("ix_ai_usage_entries_priced_at", "priced_at"),
        Index("ix_ai_usage_entries_reporter_api_key_id", "reporter_api_key_id"),
        Index("ix_ai_usage_entries_tool_slug", "tool_slug"),
        Index("ix_ai_usage_entries_subscription_id", "subscription_id"),
        Index("ix_ai_usage_entries_price_id", "price_id"),
        Index("ix_ai_usage_entries_excluded_by_id", "excluded_by_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # -- attribution ---------------------------------------------------------
    project_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    issue_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("issues.id", ondelete="SET NULL"), nullable=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    reporter_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    reporter_api_key_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    attribution_method: Mapped[str] = mapped_column(String(12), nullable=False, default="none", server_default="none")

    # -- usage ---------------------------------------------------------------
    tool_slug: Mapped[str] = mapped_column(String(50), ForeignKey("ai_tools.slug", ondelete="RESTRICT"), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # -- tokens --------------------------------------------------------------
    input: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    output: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    cache_write_5m: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    cache_write_1h: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    cache_read: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    reasoning: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    #: Tokens of requests billed at the long-context tier, same keys as above.
    long_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # -- cost ----------------------------------------------------------------
    cost_type: Mapped[str] = mapped_column(String(12), nullable=False, default="unknown", server_default="unknown")
    subscription_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ai_subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    price_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ai_model_prices.id", ondelete="RESTRICT"), nullable=True
    )
    priced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    marginal_amount: Mapped[Decimal | None] = amount_column(nullable=True)
    marginal_currency: Mapped[str | None] = currency_column(nullable=True)
    api_equivalent_amount: Mapped[Decimal | None] = amount_column(nullable=True)
    api_equivalent_currency: Mapped[str | None] = currency_column(nullable=True)
    reported_cost_amount: Mapped[Decimal | None] = amount_column(nullable=True)
    reported_cost_currency: Mapped[str | None] = currency_column(nullable=True)
    cost_basis: Mapped[str] = mapped_column(String(10), nullable=False, default="unpriced", server_default="unpriced")

    # -- context -------------------------------------------------------------
    external_session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    git_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    repo_hint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    client_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- lifecycle -----------------------------------------------------------
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    excluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    excluded_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    user = relationship("User", foreign_keys=[user_id], lazy="raise")
    reporter = relationship("User", foreign_keys=[reporter_user_id], lazy="raise")
    project = relationship("Project", foreign_keys=[project_id], lazy="raise")
    issue = relationship("Issue", foreign_keys=[issue_id], lazy="raise")

    @property
    def marginal(self) -> Money | None:
        return money_or_none(self.marginal_amount, self.marginal_currency)

    @property
    def api_equivalent(self) -> Money | None:
        return money_or_none(self.api_equivalent_amount, self.api_equivalent_currency)

    @property
    def reported_cost(self) -> Money | None:
        return money_or_none(self.reported_cost_amount, self.reported_cost_currency)

    @property
    def is_locked(self) -> bool:
        """Whether only the project, issue and comments may change (reporter and import entries)."""
        return self.source in LOCKED_SOURCES

    @property
    def is_excluded(self) -> bool:
        return self.excluded_at is not None

    def tokens(self) -> dict[str, int]:
        return {c: int(getattr(self, c)) for c in TOKEN_CLASSES}

    def __repr__(self) -> str:
        return f"<AiUsageEntry id={self.id} user_id={self.user_id} model={self.model!r} date={self.usage_date}>"
