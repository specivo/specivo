"""Agent cost tracking models: ModelCostConfig, AgentTokenLog, BillingRate."""

from datetime import date
from decimal import Decimal

from sqlalchemy import BigInteger, Date, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class ModelCostConfig(Base, TimestampMixin):
    """Cost per token for a specific AI model."""

    __tablename__ = "model_cost_configs"

    __table_args__ = (UniqueConstraint("provider", "model_name", name="uq_model_cost"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    input_cost_per_1m: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    output_cost_per_1m: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)

    def __repr__(self) -> str:
        return f"<ModelCostConfig id={self.id} provider={self.provider!r} model_name={self.model_name!r}>"


class AgentTokenLog(Base, TimestampMixin):
    """Token usage log per agent session request."""

    __tablename__ = "agent_token_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, default=0)

    def __repr__(self) -> str:
        return (
            f"<AgentTokenLog id={self.id} session_id={self.session_id} model_name={self.model_name!r} cost={self.cost}>"
        )


class BillingRate(Base, TimestampMixin):
    """Hourly billing rate per user/project."""

    __tablename__ = "billing_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    hourly_rate: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)

    def __repr__(self) -> str:
        return f"<BillingRate id={self.id} hourly_rate={self.hourly_rate} currency={self.currency!r}>"
