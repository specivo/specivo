"""Tag models — lightweight per-project labels for issues and wiki pages.

A :class:`Tag` is a free-form label unique (case-insensitively) within a
project. :class:`TagLink` is the association between a tag and a single
target entity — either an issue or a wiki page — using the typed-FK pattern
(exactly one target FK set, enforced by a CHECK constraint), mirroring
:class:`specivo.models.journal.Journal`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class Tag(Base, TimestampMixin):
    """A project-scoped label. Names are unique per project, case-insensitively."""

    __tablename__ = "tags"

    __table_args__ = (
        Index("ix_tags_project_id", "project_id"),
        # Case-insensitive uniqueness per project (mirrors ix_metadata_presets_slug_lower).
        Index("uq_tags_project_name_lower", "project_id", text("lower(name)"), unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Optional display color as a hex string, e.g. "#4f9d6c".
    color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<Tag id={self.id} name={self.name!r} project_id={self.project_id}>"


class TagLink(Base):
    """Association between a tag and exactly one target entity (issue or wiki page)."""

    __tablename__ = "tag_links"

    __table_args__ = (
        # Exactly one target entity must be set.
        CheckConstraint(
            "(issue_id IS NOT NULL)::int + (wiki_page_id IS NOT NULL)::int = 1",
            name="ck_tag_links_one_target",
        ),
        # A tag may be applied to a given entity at most once. NULLs are distinct
        # in Postgres, so each constraint only guards its own entity type.
        UniqueConstraint("tag_id", "issue_id", name="uq_tag_links_tag_issue"),
        UniqueConstraint("tag_id", "wiki_page_id", name="uq_tag_links_tag_wiki_page"),
        Index("ix_tag_links_tag_id", "tag_id"),
        Index("ix_tag_links_issue_id", "issue_id"),
        Index("ix_tag_links_wiki_page_id", "wiki_page_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"),
        nullable=False,
    )
    issue_id: Mapped[int | None] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=True,
    )
    wiki_page_id: Mapped[int | None] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="CASCADE"),
        nullable=True,
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        target = f"issue_id={self.issue_id}" if self.issue_id else f"wiki_page_id={self.wiki_page_id}"
        return f"<TagLink id={self.id} tag_id={self.tag_id} {target}>"
