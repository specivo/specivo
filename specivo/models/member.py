"""Member and MemberRole models — project membership and role assignments."""

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from specivo.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from specivo.models.role import Role
    from specivo.models.user import User
    from specivo.models.user_group import UserGroup


class Member(Base, TimestampMixin):
    """Associates a principal with a project.

    The principal is **either** a user or a user group — exactly one of
    ``user_id`` / ``group_id`` is set, enforced by ``ck_members_one_principal``.
    A group-held membership works exactly like a user-held one: its roles hang
    off this row via the ``member_roles`` join table, which is unchanged and
    does not know or care which kind of principal holds the membership.

    A principal can belong to many projects; a project has many members.
    Roles live in ``member_roles`` so a member can hold several roles
    simultaneously within the same project.

    Uniqueness is expressed as two plain unique constraints rather than partial
    indexes: PostgreSQL treats NULLs as distinct, so ``uq_members_user_project``
    ignores group rows and ``uq_members_group_project`` ignores user rows, and
    the CHECK guarantees a row is never both.
    """

    __tablename__ = "members"

    __table_args__ = (
        UniqueConstraint("user_id", "project_id", name="uq_members_user_project"),
        UniqueConstraint("group_id", "project_id", name="uq_members_group_project"),
        CheckConstraint(
            "num_nonnulls(user_id, group_id) = 1",
            name="ck_members_one_principal",
        ),
        Index("ix_members_user_id", "user_id"),
        Index("ix_members_group_id", "group_id"),
        Index("ix_members_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )

    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("user_groups.id", ondelete="CASCADE"),
        nullable=True,
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    user: Mapped["User | None"] = relationship("User", foreign_keys=[user_id], lazy="raise")
    group: Mapped["UserGroup | None"] = relationship("UserGroup", foreign_keys=[group_id], lazy="raise")
    member_roles: Mapped[list["MemberRole"]] = relationship(
        "MemberRole",
        back_populates="member",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    def __repr__(self) -> str:
        principal = f"user_id={self.user_id}" if self.user_id is not None else f"group_id={self.group_id}"
        return f"<Member id={self.id} {principal} project_id={self.project_id}>"


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

    member: Mapped["Member"] = relationship(
        "Member", back_populates="member_roles", foreign_keys=[member_id], lazy="raise"
    )
    role: Mapped["Role"] = relationship("Role", foreign_keys=[role_id], lazy="raise")

    def __repr__(self) -> str:
        return f"<MemberRole id={self.id} member_id={self.member_id} role_id={self.role_id}>"
