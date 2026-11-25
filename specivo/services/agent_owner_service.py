"""The person an agent (a service account) works for: ``users.works_for_id``.

An administrator sets it. The AI usage the agent reports is then billed as that
person (:mod:`specivo.services.ai_billing_resolution`): their billing accounts,
plans, seats and budgets cover it, while the entries stay the agent's. Setting
or clearing it resolves the agent's entries again and is audited.
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, NotFoundError
from specivo.models.user import User
from specivo.services.anonymous_user_service import refuse_system_user
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

_audit = SecurityAuditService()


class AgentOwnerService:
    """Sets and clears the person a service account works for."""

    async def set_works_for(
        self,
        session: AsyncSession,
        agent: User,
        owner_id: int | None,
        *,
        actor: User,
        request: Request | None = None,
    ) -> bool:
        """Make *agent* work for *owner_id* (None: for nobody); True when it changed.

        The agent must be a service account; the owner an active person (not a
        service account, not a system principal, not the agent). The agent's
        entries are billed again; the caller commits.
        """
        from specivo.services.ai_usage_service import AiUsageService

        refuse_system_user(agent)
        if not agent.is_service_account:
            raise AppError(
                code="works_for_not_agent", message="Only a service account works for someone", status_code=422
            )
        if owner_id is not None:
            owner = await session.get(User, owner_id)
            if owner is None or owner.is_system_principal:
                raise NotFoundError(message="User not found")
            if owner.is_service_account or owner.id == agent.id or owner.status != "active":
                raise AppError(
                    code="works_for_not_person",
                    message="An agent works for an active person, not a service account",
                    status_code=422,
                )
        if agent.works_for_id == owner_id:
            return False
        previous = agent.works_for_id
        agent.works_for_id = owner_id
        await session.flush()
        await AiUsageService().rebill_for_agent(session, agent.id, actor_id=actor.id, request=request)
        await _audit.log_event(
            session=session,
            event_type=AuditEvent.MEMBER_CHANGE,
            user_id=actor.id,
            details={
                "action": "agent_works_for_changed",
                "target_user_id": agent.id,
                "target_login": agent.login,
                "works_for_id": owner_id,
                "previous_works_for_id": previous,
            },
            request=request,
        )
        return True


__all__ = ["AgentOwnerService"]
