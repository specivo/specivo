"""EntityLink model: a link written in an issue description, a comment or a wiki page."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base

# Source types: the text a link is written in.
SOURCE_ISSUE = "issue"
SOURCE_JOURNAL = "journal"
SOURCE_WIKI_PAGE = "wiki_page"
SOURCE_TYPES = (SOURCE_ISSUE, SOURCE_JOURNAL, SOURCE_WIKI_PAGE)

# Target types: what a link points at.
TARGET_ISSUE = "issue"
TARGET_WIKI_PAGE = "wiki_page"
TARGET_ATTACHMENT = "attachment"

# Kinds of text link.
KIND_WIKI_LINK = "wiki_link"  # [[Page]] / [[Page|label]]
KIND_MENTION = "mention"  # KEY-123, or a link to a page of this instance
KIND_ATTACHMENT_LINK = "attachment_link"  # a bare attachment filename as a link or image target
TEXT_KINDS = (KIND_WIKI_LINK, KIND_MENTION, KIND_ATTACHMENT_LINK)

# A link someone made on purpose, by accepting a suggested link between an issue
# and a wiki page or between two pages (two issues get an ``IssueRelation``).
# It is not parsed from text, so rebuilding a source leaves it alone.
KIND_TYPED_LINK = "typed_link"
# What a typed link says about its target, read from the source: "this page documents ...".
TYPED_LINK_TYPES = ("documents", "implements", "decided_in", "supersedes", "see_also")


class EntityLink(Base):
    """One link from a source text to an issue, wiki page or attachment.

    Text links are derived data: ``EntityLinkService.rebuild_source`` replaces
    the text-link rows of one source from its committed text, so they are never edited
    in place except to resolve a target that did not exist yet.

    Typed foreign keys
    ------------------
    Like ``journals``, the source and the target are separate typed FK columns
    rather than a polymorphic ``(type, id)`` pair, so deleting a source removes
    its links in the database (CASCADE) and deleting a target turns its links
    into unresolved ones (SET NULL). ``target_ref`` keeps the target as written
    (a slug, ``KEY-N`` or a filename), so an unresolved link resolves when the
    target is created or renamed to match.

    Structured connections (relations, parent/child, attachments, commits and
    pull requests) live in their own tables and are merged at read time.

    One kind is not derived: ``typed_link`` rows are written when someone
    accepts a suggested link (``link_suggestion_service``) and are never
    touched by a rebuild. They come from an issue or a wiki page, never from a
    comment, and carry a ``link_type``.
    """

    __tablename__ = "entity_links"

    __table_args__ = (
        CheckConstraint(
            "(source_issue_id IS NOT NULL)::int + (source_journal_id IS NOT NULL)::int"
            " + (source_wiki_page_id IS NOT NULL)::int = 1",
            name="ck_entity_links_one_source",
        ),
        CheckConstraint(
            "(target_issue_id IS NOT NULL)::int + (target_wiki_page_id IS NOT NULL)::int"
            " + (target_attachment_id IS NOT NULL)::int <= 1",
            name="ck_entity_links_one_target",
        ),
        CheckConstraint(
            "(kind = 'typed_link') = (link_type IS NOT NULL)"
            " AND (link_type IS NULL OR link_type IN"
            " ('documents', 'implements', 'decided_in', 'supersedes', 'see_also'))",
            name="ck_entity_links_link_type",
        ),
        Index(
            "uq_entity_links_source_target",
            "source_issue_id",
            "source_journal_id",
            "source_wiki_page_id",
            "kind",
            "target_type",
            "target_project_id",
            "target_ref",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_entity_links_source_issue_id", "source_issue_id"),
        Index("ix_entity_links_source_journal_id", "source_journal_id"),
        Index("ix_entity_links_source_wiki_page_id", "source_wiki_page_id"),
        Index("ix_entity_links_source_project_id", "source_project_id"),
        Index("ix_entity_links_target_issue_id", "target_issue_id"),
        Index("ix_entity_links_target_wiki_page_id", "target_wiki_page_id"),
        Index("ix_entity_links_target_attachment_id", "target_attachment_id"),
        Index("ix_entity_links_target_project_id", "target_project_id"),
        Index(
            "ix_entity_links_unresolved",
            "target_type",
            "target_project_id",
            "target_ref",
            postgresql_where=text(
                "target_issue_id IS NULL AND target_wiki_page_id IS NULL AND target_attachment_id IS NULL"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), nullable=True)
    source_journal_id: Mapped[int | None] = mapped_column(ForeignKey("journals.id", ondelete="CASCADE"), nullable=True)
    source_wiki_page_id: Mapped[int | None] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=True
    )
    source_project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id", ondelete="SET NULL"), nullable=True)
    target_wiki_page_id: Mapped[int | None] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="SET NULL"), nullable=True
    )
    target_attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("attachments.id", ondelete="SET NULL"), nullable=True
    )
    target_project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True)
    target_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    display_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Only for ``typed_link``: one of ``TYPED_LINK_TYPES``.
    link_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    @property
    def is_resolved(self) -> bool:
        return (
            self.target_issue_id is not None
            or self.target_wiki_page_id is not None
            or self.target_attachment_id is not None
        )

    def __repr__(self) -> str:
        return (
            f"<EntityLink id={self.id} {self.source_type} -> {self.target_type} "
            f"{self.kind} ref={self.target_ref!r} resolved={self.is_resolved}>"
        )
