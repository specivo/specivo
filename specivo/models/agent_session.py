"""AgentSession model — tracks agent work sessions via API key usage."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class AgentSession(Base, TimestampMixin):
    """One activity window of an MCP connection: tool calls less than the session timeout apart.

    ``connection_hash`` is the SHA-256 hex digest of the MCP transport session
    id (``Mcp-Session-Id``, or the session id of a legacy SSE stream), so each
    client connection has sessions of its own even when several share one API
    key. It is ``NULL`` for plain REST calls, which share one window per key.

    ``client_name`` / ``client_version`` name the client that opened the
    window: the MCP ``clientInfo`` (for example ``claude-code`` / ``2.1.0``),
    else the first product of the HTTP User-Agent. They never name a model;
    the model is recorded on each AI usage entry.

    ``issue_id`` is the issue the account was last seen starting work on in
    this window through MCP (moving it to an ``active`` status, assigning it to
    itself, starting a timer on it). It is an attribution hint for AI usage
    ingest, not a permission.

    ``tool_call_count`` counts the MCP tool calls written to the security
    audit log for this session (``security_audit_logs.agent_session_id``), so
    the session list needs no join to the partitioned audit table.
    """

    __tablename__ = "agent_sessions"
    __table_args__ = (
        Index("ix_agent_sessions_api_key_id_last_activity_at", "api_key_id", "last_activity_at"),
        Index("ix_agent_sessions_user_id_last_activity_at", "user_id", "last_activity_at"),
        Index(
            "ix_agent_sessions_api_key_id_connection_hash_last_activity_at",
            "api_key_id",
            "connection_hash",
            "last_activity_at",
        ),
        Index("ix_agent_sessions_user_external", "user_id", "external_session_id"),
        Index("ix_agent_sessions_parent_session_id", "parent_session_id"),
        CheckConstraint("client_type IN ('mcp', 'rest', 'webhook')", name="ck_agent_sessions_client_type"),
        CheckConstraint(
            "api_key_id IS NOT NULL OR client_type = 'webhook'", name="ck_agent_sessions_api_key_unless_webhook"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    api_key_id: Mapped[int | None] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    issue_id: Mapped[int | None] = mapped_column(
        ForeignKey("issues.id", ondelete="SET NULL"), nullable=True, index=True
    )
    connection_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    client_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    external_session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    parent_external_session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    parent_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="SET NULL", name="fk_agent_sessions_parent_session_id"),
        nullable=True,
    )
    client_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    def __repr__(self) -> str:
        return (
            f"<AgentSession id={self.id} api_key_id={self.api_key_id} "
            f"client={self.client_name!r} {self.client_version!r} external={self.external_session_id!r}>"
        )
