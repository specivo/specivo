"""Attachment model — files uploaded to issues, wiki pages, or journals."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from specivo.models.base import Base, TimestampMixin


class Attachment(Base, TimestampMixin):
    """A file attached to a tracker entity (issue, wiki page, journal).

    Storage
    -------
    Files are saved on the local filesystem (default) or S3-compatible storage.
    The ``disk_filename`` is UUID-based to prevent collisions and path traversal.
    The ``filename`` column preserves the original name as uploaded.

    Polymorphic container
    ---------------------
    ``container_type`` + ``container_id`` form a polymorphic association
    (no FK constraint on container_id — see Tracker_Attachments Section 3).
    Orphan cleanup is done by a periodic task.  This pattern allows attaching
    to any entity without schema changes.

    Allowed ``container_type`` values: ``"Issue"``, ``"WikiPage"``, ``"Journal"``.
    """

    __tablename__ = "attachments"

    __table_args__ = (
        # Composite index for "list attachments for entity X" queries
        Index("ix_attachments_container", "container_type", "container_id"),
        Index("ix_attachments_author_id", "author_id"),
        # GIN index on metadata JSONB column created by migration 0002
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    container_type: Mapped[str] = mapped_column(String(30), nullable=False)
    container_id: Mapped[int] = mapped_column(Integer, nullable=False)

    # Original filename as uploaded by the user / agent
    filename: Mapped[str] = mapped_column(String(255), nullable=False)

    # UUID-based name on disk/S3 — e.g. "a3f8c2d1-…-ef0123456789.pdf"
    disk_filename: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)

    filesize: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")

    author_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    description: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # SHA-256 hex digest of the file content (64 chars). Populated on upload.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Structured metadata (image dims, PDF info, extracted text, etc.)
    # Column name in DB is "metadata"; Python attribute is "file_metadata" to
    # avoid clash with SQLAlchemy's reserved `metadata` on DeclarativeBase.
    file_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    author = relationship("User", foreign_keys=[author_id], lazy="raise")

    def __repr__(self) -> str:
        return (
            f"<Attachment id={self.id} container={self.container_type}/{self.container_id} filename={self.filename!r}>"
        )


# Expose `file_metadata` as `.metadata` on instances without conflicting with
# SQLAlchemy's reserved class-level `metadata` attribute (MetaData object).
# The property is monkey-patched after class creation so it is not seen by
# the declarative metaclass scanner.
def _metadata_getter(self: Attachment) -> dict | None:
    return self.file_metadata


def _metadata_setter(self: Attachment, value: dict | None) -> None:
    self.file_metadata = value


Attachment.metadata = property(_metadata_getter, _metadata_setter)  # type: ignore[assignment,attr-defined]
