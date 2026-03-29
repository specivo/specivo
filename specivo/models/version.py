"""Version model — project milestones and releases for the roadmap."""

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class Version(Base, TimestampMixin):
    """A project version (milestone / release target).

    ``status`` values:
    - ``open``   — actively accepting issues
    - ``locked`` — no further changes; issues can still be moved off it
    - ``closed`` — completed; shown in changelog but not in open roadmap

    ``sharing`` controls which projects can assign issues to this version:
    - ``none``        — only this project
    - ``descendants`` — this project + all subprojects
    - ``hierarchy``   — all projects in the ancestor–descendant chain
    - ``tree``        — the whole project tree (root + all descendants)
    - ``system``      — every project in the system

    ``effective_date``: the target completion / release date.
    ``wiki_page_title``: optional link to a wiki page describing this version.
    """

    __tablename__ = "versions"

    __table_args__ = (
        Index("ix_versions_project_id", "project_id"),
        CheckConstraint(
            "status IN ('open', 'locked', 'closed')",
            name="ck_versions_status",
        ),
        CheckConstraint(
            "sharing IN ('none', 'descendants', 'hierarchy', 'tree', 'system')",
            name="ck_versions_sharing",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(30), nullable=False, default="open", server_default="open")

    effective_date: Mapped[object] = mapped_column(Date, nullable=True)

    sharing: Mapped[str] = mapped_column(String(30), nullable=False, default="none", server_default="none")

    wiki_page_title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:
        return f"<Version id={self.id} name={self.name!r} project_id={self.project_id} status={self.status!r}>"
