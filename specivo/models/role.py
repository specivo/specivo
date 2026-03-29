"""Role model for RBAC permission system."""

from sqlalchemy import Boolean, CheckConstraint, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class Role(Base, TimestampMixin):
    """Project role with a set of permissions.

    Roles are assigned to project members via the MemberRole join table.

    ``builtin`` values:
    - 0: custom role (assignable via the UI)
    - 1: non-member (applied to users who access a public project without
         being explicit members)
    - 2: anonymous (applied to unauthenticated users on public projects)

    ``permissions``: list of permission string constants, e.g.
    ``["add_issues", "edit_issues"]``. Use ``["*"]`` to grant all permissions
    (Manager role).

    ``issues_visibility``:
    - ``"default"`` - respects project visibility rules
    - ``"all"``     - can see all issues including private
    - ``"own"``     - can see only own issues
    """

    __tablename__ = "roles"

    __table_args__ = (
        CheckConstraint(
            "builtin IN (0, 1, 2)",
            name="ck_roles_builtin",
        ),
        CheckConstraint(
            "issues_visibility IN ('default', 'all', 'own')",
            name="ck_roles_issues_visibility",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Unique human-readable name (e.g. "Manager", "Developer")
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    # Display ordering in role lists; lower = first
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # Whether this role can be assigned to project members via the UI
    assignable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    # 0=custom, 1=non_member, 2=anonymous
    builtin: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # List of permission strings granted by this role.  ["*"] = all permissions.
    permissions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")

    # Issue visibility scope for this role
    issues_visibility: Mapped[str] = mapped_column(
        String(30), nullable=False, default="default", server_default="default"
    )

    # Extensible settings JSONB (reserved for future use, e.g. Phase 2 workflow)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    def __repr__(self) -> str:
        return f"<Role id={self.id} name={self.name!r} builtin={self.builtin}>"
