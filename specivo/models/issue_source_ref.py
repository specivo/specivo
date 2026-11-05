"""The number an imported issue had in the system it came from.

``import_id_map`` remembers the same issues, but for re-running an import: it
holds the source's internal id, it names the source installation, and an
operator purges it after cut-over. This table is what readers see and search,
so it keeps only the number people know, for as long as the issue exists.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base


class IssueSourceRef(Base):
    """One number that means this issue in its source system.

    An issue has one primary number, and an alias for each other number that
    now means it (a ticket merged into it, a key it had before moving).
    ``(source_system, ref)`` is not unique: two installations of a tracker can
    both have a ticket 123.
    """

    __tablename__ = "issue_source_refs"

    __table_args__ = (
        # Starts with issue_id, so it is also the foreign key's index.
        UniqueConstraint("issue_id", "source_system", "ref", name="uq_issue_source_refs_issue_system_ref"),
        Index("ix_issue_source_refs_system_ref", "source_system", func.lower(text("ref"))),
        Index("ix_issue_source_refs_ref", func.lower(text("ref"))),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_id: Mapped[int] = mapped_column(Integer, ForeignKey("issues.id", ondelete="CASCADE"), nullable=False)
    # The bundle's source system ("rt", "jira"); core never interprets it.
    source_system: Mapped[str] = mapped_column(String(30), nullable=False)
    # The number people know: "123", "PROJ-7".
    ref: Mapped[str] = mapped_column(String(255), nullable=False)
    # How it is shown: "RT #123", "PROJ-7".
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    # False for a number merged into this issue.
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"<IssueSourceRef issue={self.issue_id} {self.source_system}:{self.ref}>"
