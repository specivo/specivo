"""Member and MemberRole models — project membership and role assignments."""

from sqlalchemy import ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class Member(Base, TimestampMixin):
    """Associates a user with a project.

    A user can belong to many projects; a project has many members.
    Roles are stored in the ``member_roles`` join table so a member can
    hold multiple roles simultaneously within the same project.
    """

    __tablename__ = "members"

    __table_args__ = (
        UniqueConstraint("user_id", "project_id", name="uq_members_user_project"),
        Index("ix_members_user_id", "user_id"),
        Index("ix_members_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Member id={self.id} user_id={self.user_id} project_id={self.project_id}>"


class MemberRole(Base):
    """Assigns a role to a project member.

    ``inherited_from``: the ``member_roles.id`` of the ancestor membership
    from which this role was propagated (``inherit_members`` flag on the
    parent project).  ``NULL`` means the role was assigned directly.
    """

    __tablename__ = "member_roles"

    __table_args__ = (
        UniqueConstraint("member_id", "role_id", name="uq_member_roles_member_role"),
        Index("ix_member_roles_role_id", "role_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    member_id: Mapped[int] = mapped_column(
        ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
    )

    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
    )

    # member_roles.id from the ancestor project, or NULL for direct assignment
    inherited_from: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<MemberRole id={self.id} member_id={self.member_id} role_id={self.role_id}>"
