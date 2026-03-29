"""AgentSessionService — manage agent work sessions tied to API key usage."""

from __future__ import annotations

import logging
import re
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.utils import utcnow
from specivo.models.agent_session import AgentSession

logger = logging.getLogger(__name__)

# Session inactivity threshold: if last activity was more than this many seconds
# ago, start a new session instead of reusing the old one.
# Resolved lazily to avoid import-time get_settings() (breaks CI/test imports).
_SESSION_TIMEOUT_SECONDS: int | None = None


def _get_session_timeout() -> int:
    global _SESSION_TIMEOUT_SECONDS
    if _SESSION_TIMEOUT_SECONDS is None:
        _SESSION_TIMEOUT_SECONDS = get_settings().agent_session_timeout
    return _SESSION_TIMEOUT_SECONDS


# Patterns to extract model name from User-Agent header.
# Examples:
#   "claude-code/1.0 (Claude opus-4)" -> "claude opus-4"
#   "Claude/opus-4" -> "opus-4"
#   "claude-code/1.0" -> "claude-code"
_MODEL_PATTERNS = [
    re.compile(r"\(Claude\s+(.+?)\)", re.IGNORECASE),  # (Claude opus-4)
    re.compile(r"Claude/(\S+)", re.IGNORECASE),  # Claude/opus-4
    re.compile(r"(claude-code)\b", re.IGNORECASE),  # claude-code (fallback)
]


def parse_model_name(user_agent: str | None) -> str | None:
    """Extract AI model name from User-Agent string.

    Returns None if no recognizable pattern is found.
    """
    if not user_agent:
        return None
    for pattern in _MODEL_PATTERNS:
        match = pattern.search(user_agent)
        if match:
            return match.group(1).strip()
    return None


class AgentSessionService:
    """Stateless service for agent session management."""

    async def get_or_create_session(
        self,
        session: AsyncSession,
        api_key_id: int,
        user_id: int,
        user_agent: str | None = None,
    ) -> AgentSession:
        """Find an active session for this API key or create a new one.

        A session is considered "active" if its last_activity_at is within
        the timeout window. Otherwise a new session is started.
        """
        now = utcnow()

        # Look for the most recent session for this API key
        stmt = (
            select(AgentSession)
            .where(AgentSession.api_key_id == api_key_id)
            .order_by(AgentSession.last_activity_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing is not None:
            elapsed = (now - existing.last_activity_at.replace(tzinfo=UTC)).total_seconds()
            if elapsed < _get_session_timeout():
                # Update activity timestamp
                existing.last_activity_at = now
                await session.flush()
                return existing

        # Create new session
        model_name = parse_model_name(user_agent)
        agent_session = AgentSession(
            api_key_id=api_key_id,
            user_id=user_id,
            model_name=model_name,
            started_at=now,
            last_activity_at=now,
        )
        session.add(agent_session)
        await session.flush()

        logger.debug(
            "Created agent session %d for api_key_id=%d model=%s",
            agent_session.id,
            api_key_id,
            model_name,
        )
        return agent_session

    async def update_activity(self, session: AsyncSession, session_id: int) -> None:
        """Update last_activity_at timestamp for a session."""
        stmt = select(AgentSession).where(AgentSession.id == session_id)
        result = await session.execute(stmt)
        agent_session = result.scalar_one_or_none()
        if agent_session is not None:
            agent_session.last_activity_at = utcnow()
            await session.flush()

    async def list_for_project(
        self,
        session: AsyncSession,
        project_id: int,
        limit: int = 25,
    ) -> list[AgentSession]:
        """List recent agent sessions for a project.

        Joins through api_keys -> users -> members to find sessions
        associated with the project. For simplicity, returns all recent
        sessions (agent sessions are not directly project-scoped; they
        are scoped by API key which may have project-scoped access).
        """
        # For now, return recent sessions. Project scoping can be refined
        # when API key scopes enforce project-level access.
        stmt = select(AgentSession).order_by(AgentSession.last_activity_at.desc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())
