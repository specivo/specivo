"""User group models — named sets of users that can hold project memberships.

A ``UserGroup`` is a principal in its own right: it can be added to a project
the same way a user can, and the roles granted to it apply to every user in
it. This is separate from ``AgentGroup``, which is an AI-agent access-policy
construct and has nothing to do with project membership.
"""

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from specivo.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from specivo.models.user import User


class UserGroup(Base, TimestampMixin):
    """A named group of users that can be granted roles on a project.

    Indexes:
    - uq_user_groups_name_ci: case-insensitive unique on LOWER(name)
    """

    __tablename__ = "user_groups"

    __table_args__ = (
        # Case-insensitive uniqueness, matching how User enforces it — a plain
        # unique=True on the column would let "Developers" and "developers"
        # coexist. text() is used so autogenerate emits:
        #   CREATE UNIQUE INDEX uq_user_groups_name_ci ON user_groups (LOWER(name))
        Index("uq_user_groups_name_ci", func.lower(text("name")), unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    group_members: Mapped[list["UserGroupMember"]] = relationship(
        "UserGroupMember",
        back_populates="group",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<UserGroup id={self.id} name={self.name!r}>"


class UserGroupMember(Base, TimestampMixin):
    """Maps a user into a user group.

    Membership of the group itself; project access is granted separately by
    adding the group to a project via ``Member``.
    """

    __tablename__ = "user_group_members"

    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_user_group_members_group_user"),
        # PostgreSQL does not auto-index FK columns.
        Index("ix_user_group_members_group_id", "group_id"),
        Index("ix_user_group_members_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    group_id: Mapped[int] = mapped_column(
        ForeignKey("user_groups.id", ondelete="CASCADE"),
        nullable=False,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    group: Mapped["UserGroup"] = relationship(
        "UserGroup", back_populates="group_members", foreign_keys=[group_id], lazy="raise"
    )
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id], lazy="raise")

    def __repr__(self) -> str:
        return f"<UserGroupMember id={self.id} group_id={self.group_id} user_id={self.user_id}>"
