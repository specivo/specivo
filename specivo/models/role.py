"""Role model for RBAC permission system."""

from enum import IntEnum

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class RoleBuiltin(IntEnum):
    """Values of ``roles.builtin`` (``ck_roles_builtin``)."""

    # An ordinary role, assigned to project memberships.
    CUSTOM = 0
    # The seeded Non member role: what a signed-in user holds on a public
    # project without a membership. Exactly one row.
    NON_MEMBER = 1
    # Reserved by the CHECK constraint and never created: an anonymous
    # visitor holds a transient role built from the project's
    # ``anonymous_permissions`` instead.
    ANONYMOUS = 2


# Name the Non member role is seeded with. Administrators may not rename it
# through the application, but code finds it by ``builtin``, never by name.
NON_MEMBER_ROLE_NAME = "Non member"

# ``roles.users_visibility`` values (``ck_roles_users_visibility``), Redmine's
# own spelling so imported values carry over unchanged.
USERS_VISIBILITY_ALL = "all"
USERS_VISIBILITY_MEMBERS = "members_of_visible_projects"
USERS_VISIBILITY_VALUES: tuple[str, ...] = (USERS_VISIBILITY_ALL, USERS_VISIBILITY_MEMBERS)

# ``roles.time_entries_visibility`` values (``ck_roles_time_entries_visibility``).
TIME_ENTRIES_VISIBILITY_ALL = "all"
TIME_ENTRIES_VISIBILITY_OWN = "own"
TIME_ENTRIES_VISIBILITY_VALUES: tuple[str, ...] = (TIME_ENTRIES_VISIBILITY_ALL, TIME_ENTRIES_VISIBILITY_OWN)


class Role(Base, TimestampMixin):
    """Project role with a set of permissions.

    Roles are assigned to project members via the MemberRole join table.

    ``builtin`` holds a ``RoleBuiltin`` value. Builtin roles (``builtin > 0``)
    are system-managed, and the database enforces it:
    ``uq_roles_single_builtin`` allows one row per kind,
    ``ck_roles_builtin_not_assignable`` keeps them unassignable, the
    ``trg_protect_builtin_roles`` trigger refuses deleting them or changing any
    role's ``builtin``, and ``trg_reject_builtin_role_membership`` keeps them
    out of ``member_roles``. Their permissions remain editable.

    ``permissions``: list of permission string constants, e.g.
    ``["add_issues", "edit_issues"]``. Use ``["*"]`` to grant all permissions
    (Manager role).

    ``issues_visibility``:
    - ``"default"`` - non-private issues, plus private ones the user authored or is assigned to
    - ``"all"``     - currently the same as ``"default"``
    - ``"own"``     - only issues the user authored or is assigned to

    ``users_visibility``:
    - ``"all"``                         - every active user
    - ``"members_of_visible_projects"`` - the user themselves and the members
      of projects the user can reach

    ``time_entries_visibility``:
    - ``"all"`` - every time entry in the project
    - ``"own"`` - only the entries the user logged

    Both default to ``"all"``. When a user holds several roles the most
    permissive value wins; see ``permission_service``.
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
        CheckConstraint(
            "users_visibility IN ('all', 'members_of_visible_projects')",
            name="ck_roles_users_visibility",
        ),
        CheckConstraint(
            "time_entries_visibility IN ('all', 'own')",
            name="ck_roles_time_entries_visibility",
        ),
        CheckConstraint(
            "builtin = 0 OR NOT assignable",
            name="ck_roles_builtin_not_assignable",
        ),
        Index(
            "uq_roles_single_builtin",
            "builtin",
            unique=True,
            postgresql_where=text("builtin > 0"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Unique human-readable name (e.g. "Manager", "Developer")
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    # Display ordering in role lists; lower = first
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # Whether this role can be assigned to project members via the UI
    assignable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    # A RoleBuiltin value; immutable once the row exists
    builtin: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # List of permission strings granted by this role.  ["*"] = all permissions.
    permissions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")

    # Issue visibility scope for this role
    issues_visibility: Mapped[str] = mapped_column(
        String(30), nullable=False, default="default", server_default="default"
    )

    # Which users this role may list: every user, or members of reachable projects
    users_visibility: Mapped[str] = mapped_column(
        String(30), nullable=False, default=USERS_VISIBILITY_ALL, server_default=USERS_VISIBILITY_ALL
    )

    # Which time entries this role may read in a project: all, or the user's own
    time_entries_visibility: Mapped[str] = mapped_column(
        String(30), nullable=False, default=TIME_ENTRIES_VISIBILITY_ALL, server_default=TIME_ENTRIES_VISIBILITY_ALL
    )

    # Extensible settings JSONB (reserved for future use, e.g. Phase 2 workflow)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    def __repr__(self) -> str:
        return f"<Role id={self.id} name={self.name!r} builtin={self.builtin}>"
