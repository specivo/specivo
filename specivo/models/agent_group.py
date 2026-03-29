"""Agent group models for group-based access policies."""

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class AgentGroup(Base, TimestampMixin):
    """Named group of agents/users for policy-based access control."""

    __tablename__ = "agent_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentGroupMembership(Base, TimestampMixin):
    """Maps users to agent groups."""

    __tablename__ = "agent_group_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("agent_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_agent_group_member"),)


class GroupPolicy(Base, TimestampMixin):
    """Access policy attached to an agent group.

    - ``project_id``: ``None`` means the policy applies globally.
    - ``scopes``: list of scope strings, e.g. ``["read", "write", "admin"]``.
    - ``ip_allowlist``: optional CIDR list, e.g. ``["10.0.0.0/8"]``.
    """

    __tablename__ = "group_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("agent_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    scopes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    ip_allowlist: Mapped[list | None] = mapped_column(JSONB, nullable=True)
