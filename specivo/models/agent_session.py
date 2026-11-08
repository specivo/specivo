"""AgentSession model — tracks agent work sessions via API key usage."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class AgentSession(Base, TimestampMixin):
    """One activity window of an API key: requests less than the session timeout apart.

    ``client_name`` / ``client_version`` name the client that opened the
    window: the MCP ``clientInfo`` (for example ``claude-code`` / ``2.1.0``),
    else the first product of the HTTP User-Agent. They never name a model;
    the model is recorded on each AI usage entry.

    ``issue_id`` is the issue the account was last seen starting work on in
    this window through MCP (moving it to an ``active`` status, assigning it to
    itself, starting a timer on it). It is an attribution hint for AI usage
    ingest, not a permission.
    """

    __tablename__ = "agent_sessions"
    __table_args__ = (
        Index("ix_agent_sessions_api_key_id_last_activity_at", "api_key_id", "last_activity_at"),
        Index("ix_agent_sessions_user_id_last_activity_at", "user_id", "last_activity_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    issue_id: Mapped[int | None] = mapped_column(
        ForeignKey("issues.id", ondelete="SET NULL"), nullable=True, index=True
    )
    client_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    client_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<AgentSession id={self.id} api_key_id={self.api_key_id} "
            f"client={self.client_name!r} {self.client_version!r}>"
        )
