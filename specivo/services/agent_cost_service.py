"""Agent cost service — token logging and cost aggregation."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError
from specivo.models.agent_cost import AgentTokenLog, ModelCostConfig
from specivo.models.agent_session import AgentSession
from specivo.schemas.agent_cost import AgentTokenLogCreate, ModelCostConfigCreate

logger = logging.getLogger(__name__)

_ONE_MILLION = Decimal("1000000")


class AgentCostService:
    """Service layer for agent cost tracking."""

    # -------------------------------------------------------------------
    # ModelCostConfig CRUD
    # -------------------------------------------------------------------

    async def create_model_cost(self, session: AsyncSession, data: ModelCostConfigCreate) -> ModelCostConfig:
        config = ModelCostConfig(
            provider=data.provider,
            model_name=data.model_name,
            input_cost_per_1m=data.input_cost_per_1m,
            output_cost_per_1m=data.output_cost_per_1m,
        )
        session.add(config)
        await session.flush()
        await session.refresh(config)
        return config

    async def list_model_costs(self, session: AsyncSession) -> list[ModelCostConfig]:
        result = await session.execute(
            select(ModelCostConfig).order_by(ModelCostConfig.provider, ModelCostConfig.model_name)
        )
        return list(result.scalars().all())

    async def delete_model_cost(self, session: AsyncSession, config_id: int) -> None:
        result = await session.execute(select(ModelCostConfig).where(ModelCostConfig.id == config_id))
        config = result.scalar_one_or_none()
        if config is None:
            raise NotFoundError(f"Model cost config {config_id} not found")
        await session.delete(config)
        await session.flush()

    # -------------------------------------------------------------------
    # Token logging
    # -------------------------------------------------------------------

    async def log_tokens(
        self,
        session: AsyncSession,
        data: AgentTokenLogCreate,
    ) -> AgentTokenLog:
        """Log token usage and calculate cost from ModelCostConfig."""
        # Look up cost config
        result = await session.execute(select(ModelCostConfig).where(ModelCostConfig.model_name == data.model_name))
        config = result.scalar_one_or_none()

        if config is not None:
            cost = (
                Decimal(data.input_tokens) / _ONE_MILLION * config.input_cost_per_1m
                + Decimal(data.output_tokens) / _ONE_MILLION * config.output_cost_per_1m
            )
        else:
            cost = Decimal("0")
            logger.warning("No cost config for model %r, cost set to 0", data.model_name)

        log = AgentTokenLog(
            session_id=data.session_id,
            model_name=data.model_name,
            input_tokens=data.input_tokens,
            output_tokens=data.output_tokens,
            cost=cost,
        )
        session.add(log)
        await session.flush()
        await session.refresh(log)
        return log

    # -------------------------------------------------------------------
    # Cost aggregation
    # -------------------------------------------------------------------

    async def get_cost_per_issue(self, session: AsyncSession, issue_id: int) -> dict:
        """Get total cost across all sessions for a given issue."""
        result = await session.execute(
            select(
                func.coalesce(func.sum(AgentTokenLog.input_tokens), 0).label("total_input_tokens"),
                func.coalesce(func.sum(AgentTokenLog.output_tokens), 0).label("total_output_tokens"),
                func.coalesce(func.sum(AgentTokenLog.cost), Decimal("0")).label("total_cost"),
            )
            .join(AgentSession, AgentTokenLog.session_id == AgentSession.id)
            .where(AgentSession.issue_id == issue_id)
        )
        row = result.one()
        return {
            "total_input_tokens": row.total_input_tokens,
            "total_output_tokens": row.total_output_tokens,
            "total_cost": row.total_cost,
        }

    async def get_cost_summary(
        self,
        session: AsyncSession,
        project_id: int,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[dict]:
        """Get aggregated costs by model for a project."""
        from specivo.models.issue import Issue

        query = (
            select(
                AgentTokenLog.model_name,
                func.sum(AgentTokenLog.input_tokens).label("total_input_tokens"),
                func.sum(AgentTokenLog.output_tokens).label("total_output_tokens"),
                func.sum(AgentTokenLog.cost).label("total_cost"),
            )
            .join(AgentSession, AgentTokenLog.session_id == AgentSession.id)
            .join(Issue, AgentSession.issue_id == Issue.id)
            .where(Issue.project_id == project_id)
            .group_by(AgentTokenLog.model_name)
        )

        if date_from is not None:
            query = query.where(func.date(AgentTokenLog.created_at) >= date_from)
        if date_to is not None:
            query = query.where(func.date(AgentTokenLog.created_at) <= date_to)

        result = await session.execute(query)
        return [
            {
                "model_name": row.model_name,
                "total_input_tokens": row.total_input_tokens,
                "total_output_tokens": row.total_output_tokens,
                "total_cost": row.total_cost,
            }
            for row in result.all()
        ]
