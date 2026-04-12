"""MetadataSchema model — JSON Schema definitions for issue metadata validation."""

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from specivo.models.base import Base, TimestampMixin


class MetadataSchema(Base, TimestampMixin):
    """A JSON Schema definition that validates issue metadata.

    Schemas are scoped to a project, and optionally to a specific tracker.
    When ``tracker_id`` is NULL, the schema applies to ALL trackers in
    the project (project-wide schema).

    Both project-wide and tracker-specific schemas are validated against
    the issue metadata — the metadata must satisfy all matching schemas.
    """

    __tablename__ = "metadata_schemas"

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "tracker_id",
            "name",
            "content_type",
            name="uq_metadata_schema_project_tracker_name",
        ),
        Index("ix_metadata_schemas_project_id", "project_id"),
        Index("ix_metadata_schemas_tracker_id", "tracker_id"),
        Index("ix_metadata_schemas_content_type", "content_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    tracker_id: Mapped[int | None] = mapped_column(
        ForeignKey("trackers.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # Content type discriminator — allows schemas to target different
    # entity kinds (issue, wiki, sprint, ...).  Plugins register new
    # values via ``MetadataTargetRegistry``.
    content_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="issue",
        server_default="issue",
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    schema_definition: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Links back to the preset that created this schema (NULL for custom schemas)
    preset_slug: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Relationships (lazy="raise" — project pattern)
    project = relationship("Project", foreign_keys=[project_id], lazy="raise")
    tracker = relationship("Tracker", foreign_keys=[tracker_id], lazy="raise")

    def __repr__(self) -> str:
        return (
            f"<MetadataSchema id={self.id} name={self.name!r} "
            f"project_id={self.project_id} tracker_id={self.tracker_id}>"
        )
