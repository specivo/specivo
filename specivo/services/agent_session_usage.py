"""The AI usage entries that belong to an agent session, as a viewer may read them.

An entry belongs to a session of its own user:

- when the session carries the client's session id (``external_session_id``),
  by that id: the reporter and ``specivo_log_ai_usage`` send it with the usage;
- otherwise by time: an entry without a client session id whose
  ``[started_at, ended_at]`` overlaps the session's window
  ``[started_at, ended_at or last_activity_at]``. Entries without a start time
  are whole-day buckets and are not placed in a session this way.

Several session rows can share a client session id (a client that reconnects
opens a new row with the same id); each of them shows the entries of that id.
Visibility is ``AiUsageService.visible_entries_clause_any``: the viewer sees
only the entries they may read anywhere, whatever the session.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from specivo.core.money import Money
from specivo.models.agent_session import AgentSession
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.user import User
from specivo.services.ai_usage_service import AiUsageService

_usage = AiUsageService()


@dataclass
class SessionUsage:
    """The counted usage of one agent session: entries and marginal spend per currency."""

    entries: int = 0
    spend: list[Money] = field(default_factory=list)


def session_match() -> ColumnElement[bool]:
    """The join condition between ``AiUsageEntry`` and the ``AgentSession`` it belongs to (see module docstring)."""
    session_end = func.coalesce(AgentSession.ended_at, AgentSession.last_activity_at)
    by_external = and_(
        AgentSession.external_session_id.is_not(None),
        AiUsageEntry.external_session_id == AgentSession.external_session_id,
    )
    by_time = and_(
        AgentSession.external_session_id.is_(None),
        AiUsageEntry.external_session_id.is_(None),
        AiUsageEntry.started_at.is_not(None),
        AiUsageEntry.started_at <= session_end,
        func.coalesce(AiUsageEntry.ended_at, AiUsageEntry.started_at) >= AgentSession.started_at,
    )
    return and_(AiUsageEntry.user_id == AgentSession.user_id, or_(by_external, by_time))


async def usage_by_session(
    session: AsyncSession, viewer: User, agent_session_ids: Sequence[int]
) -> dict[int, SessionUsage]:
    """The counted entries and marginal spend of each session, as *viewer* may read them.

    Two grouped queries for the whole page of sessions. Excluded entries are
    left out, as in every total. Sessions without usage are missing from the map.
    """
    if not agent_session_ids:
        return {}
    conditions: list[ColumnElement[bool]] = [
        AgentSession.id.in_(list(agent_session_ids)),
        AiUsageEntry.excluded_at.is_(None),
    ]
    visible = await _usage.visible_entries_clause_any(session, viewer)
    if visible is not None:
        conditions.append(visible)
    joined = select(AgentSession.id).join(AiUsageEntry, session_match())

    usage: dict[int, SessionUsage] = {}
    counts = await session.execute(
        joined.add_columns(func.count(AiUsageEntry.id)).where(*conditions).group_by(AgentSession.id)
    )
    for session_id, count in counts.all():
        usage[session_id] = SessionUsage(entries=int(count))
    spend = await session.execute(
        joined.add_columns(AiUsageEntry.marginal_currency, func.sum(AiUsageEntry.marginal_amount))
        .where(*conditions, AiUsageEntry.marginal_amount.is_not(None), AiUsageEntry.marginal_currency.is_not(None))
        .group_by(AgentSession.id, AiUsageEntry.marginal_currency)
        .order_by(AgentSession.id, AiUsageEntry.marginal_currency)
    )
    for session_id, currency, total in spend.all():
        usage.setdefault(session_id, SessionUsage()).spend.append(Money(total, currency))
    return usage


async def session_entries(
    session: AsyncSession, viewer: User, agent_session: AgentSession, offset: int = 0, limit: int = 50
) -> tuple[list[AiUsageEntry], int]:
    """The entries of *agent_session* that *viewer* may read, newest day first. Returns ``(page, total)``.

    Excluded entries are listed too (the table marks them), as on the AI usage pages.
    """
    belongs = select(AgentSession.id).where(AgentSession.id == agent_session.id, session_match()).exists()
    conditions: list[ColumnElement[bool]] = [belongs]
    visible = await _usage.visible_entries_clause_any(session, viewer)
    if visible is not None:
        conditions.append(visible)
    total = await session.scalar(select(func.count()).select_from(AiUsageEntry).where(*conditions)) or 0
    rows = await session.execute(
        select(AiUsageEntry)
        .where(*conditions)
        .order_by(AiUsageEntry.usage_date.desc(), AiUsageEntry.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars().all()), int(total)
