"""Project, EnabledModule, ProjectKeyAlias models for the Specivo tracker."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class Project(Base, TimestampMixin):
    """Tracker project.

    ``status`` values:
    - 1: active
    - 5: closed
    - 9: archived

    ``path``: ltree label path, e.g. ``"specivo.tracker"``.  Stored as Text;
    a GiST index with ltree_ops is added in ``__table_args__`` so PostgreSQL
    can use ltree operators for ancestor/descendant queries.

    ``key``: uppercase project key (e.g. ``SPV``).  Used as issue prefix:
    ``SPV-42``.  Constrained to ``^[A-Z][A-Z0-9]{1,9}$`` via CHECK.

    ``issue_sequence``: monotonically-increasing counter for issue numbers
    within this project.  Updated atomically via UPDATE … RETURNING.
    """

    __tablename__ = "projects"

    __table_args__ = (
        # GiST index using ltree_ops so PostgreSQL can use ltree operators
        Index("ix_projects_path_gist", "path", postgresql_using="gist"),
        Index("ix_projects_identifier", "identifier"),
        Index("ix_projects_parent_id", "parent_id"),
        CheckConstraint(
            "key ~ '^[A-Z][A-Z0-9]{1,9}$'",
            name="ck_projects_key_format",
        ),
        CheckConstraint(
            "status IN (1, 5, 9)",
            name="ck_projects_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # URL slug — lowercase, kebab-case, globally unique
    identifier: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    # Uppercase project key used as issue prefix (SPV, ACME, …)
    key: Mapped[str] = mapped_column(String(12), nullable=False, unique=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # ltree path stored as text; DB column is ltree type (set in migration)
    path: Mapped[str] = mapped_column(Text, nullable=False)

    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    inherit_members: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # 1=active, 5=closed, 9=archived
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # Atomic counter for PROJECT-NNN issue numbers
    issue_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # Hex color for project card border (e.g. "#c49a3c")
    color: Mapped[str | None] = mapped_column(String(7), nullable=True)

    # Extensible JSONB settings bag
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    def __repr__(self) -> str:
        return f"<Project id={self.id} key={self.key!r} identifier={self.identifier!r}>"


class EnabledModule(Base):
    """Records which modules are active for a given project.

    Module names: ``"issue_tracking"``, ``"wiki"``, ``"time_tracking"``.
    """

    __tablename__ = "enabled_modules"

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_enabled_modules_project_name"),
        Index("ix_enabled_modules_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(64), nullable=False)

    def __repr__(self) -> str:
        return f"<EnabledModule project_id={self.project_id} name={self.name!r}>"


class ProjectKeyAlias(Base):
    """Historical project key alias for redirect after rename.

    When a project key is changed (e.g. ACME → NEWCO), the old key
    is stored here so that API lookups using the retired key can
    resolve to the current project.
    """

    __tablename__ = "project_key_aliases"

    __table_args__ = (Index("ix_project_key_aliases_project_id", "project_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    old_key: Mapped[str] = mapped_column(String(12), nullable=False, unique=True)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    renamed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    renamed_by_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<ProjectKeyAlias old_key={self.old_key!r} project_id={self.project_id}>"
