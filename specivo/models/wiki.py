"""Wiki models — per-project wiki, pages, content versioning, and redirects."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from specivo.models.base import Base, LockVersionMixin, TimestampMixin
from specivo.models.user import User


class Wiki(Base, TimestampMixin):
    """Per-project wiki container. One wiki per project."""

    __tablename__ = "wikis"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, unique=True)
    # status: 1=active, 0=closed
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    def __repr__(self) -> str:
        return f"<Wiki id={self.id} project_id={self.project_id}>"


class WikiPage(Base, TimestampMixin, LockVersionMixin):
    """A single wiki page with a URL-friendly slug."""

    __tablename__ = "wiki_pages"

    __table_args__ = (
        Index(
            "uq_wiki_pages_wiki_slug_active",
            "wiki_id",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wiki_id: Mapped[int] = mapped_column(ForeignKey("wikis.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    protected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    deleted_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Relationships
    wiki: Mapped[Wiki] = relationship("Wiki", lazy="raise")
    contents: Mapped[list[WikiContent]] = relationship(
        "WikiContent",
        back_populates="page",
        lazy="raise",
        order_by="WikiContent.version.desc()",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<WikiPage id={self.id} slug={self.slug!r} wiki_id={self.wiki_id}>"


class WikiContent(Base, TimestampMixin):
    """One version of a wiki page's content. Every edit creates a new row."""

    __tablename__ = "wiki_contents"

    __table_args__ = (UniqueConstraint("page_id", "version", name="uq_wiki_contents_page_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    comments: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    page: Mapped[WikiPage] = relationship("WikiPage", back_populates="contents", lazy="raise")
    author: Mapped[User] = relationship("User", lazy="raise")

    def __repr__(self) -> str:
        return f"<WikiContent id={self.id} page_id={self.page_id} version={self.version}>"


class WikiRedirect(Base):
    """Redirect from old slug to new slug after page rename."""

    __tablename__ = "wiki_redirects"

    __table_args__ = (UniqueConstraint("wiki_id", "title_from", name="uq_wiki_redirects_from"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wiki_id: Mapped[int] = mapped_column(ForeignKey("wikis.id", ondelete="CASCADE"), nullable=False, index=True)
    title_from: Mapped[str] = mapped_column(String(255), nullable=False)
    redirected_to: Mapped[str] = mapped_column(String(255), nullable=False)

    def __repr__(self) -> str:
        return f"<WikiRedirect id={self.id} from={self.title_from!r} to={self.redirected_to!r}>"


class WikiPageLink(Base):
    """A link between wiki pages, parsed from ``[[Page_Name]]`` syntax."""

    __tablename__ = "wiki_page_links"

    __table_args__ = (
        UniqueConstraint("source_page_id", "target_slug", name="uq_wiki_page_links_source_slug"),
        Index("ix_wiki_page_links_wiki_id", "wiki_id"),
        Index("ix_wiki_page_links_target_page_id", "target_page_id"),
        Index(
            "ix_wiki_page_links_broken_target",
            "wiki_id",
            "target_slug",
            postgresql_where=text("target_page_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wiki_id: Mapped[int] = mapped_column(ForeignKey("wikis.id", ondelete="CASCADE"), nullable=False)
    source_page_id: Mapped[int] = mapped_column(ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=False)
    target_page_id: Mapped[int | None] = mapped_column(ForeignKey("wiki_pages.id", ondelete="SET NULL"), nullable=True)
    target_slug: Mapped[str] = mapped_column(String(255), nullable=False)
    display_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<WikiPageLink id={self.id} source={self.source_page_id} "
            f"target_slug={self.target_slug!r} target_page_id={self.target_page_id}>"
        )
