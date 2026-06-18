"""MetadataPreset model — built-in metadata schema templates.

Presets are seeded on first install and available to all projects.
Admins enable a preset on a project, which creates a MetadataSchema
row linked back via ``preset_slug``.

Admins can also create custom presets via the API.
"""

from sqlalchemy import Boolean, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class MetadataPreset(Base, TimestampMixin):
    """A reusable metadata schema template.

    Built-in presets are seeded by ``specivo.cli.seed``.
    Admins can create additional presets via the admin API.
    """

    __tablename__ = "metadata_presets"

    __table_args__ = (
        Index("ix_metadata_presets_slug", "slug", unique=True),
        # Case-insensitive uniqueness guarantee at the DB level, even if a write
        # path skips slug normalization.
        Index("ix_metadata_presets_slug_lower", text("lower(slug)"), unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    slug: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        comment="URL-safe identifier (e.g. 'software-development')",
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Human-readable name shown in the UI",
    )

    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="What this preset is for",
    )

    icon: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="default",
        comment="Icon identifier for the UI (e.g. 'code', 'bug', 'megaphone')",
    )

    schema_definition: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="JSON Schema defining the metadata fields",
    )

    is_builtin: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="True for presets shipped with Specivo (not deletable by admins)",
    )

    def __repr__(self) -> str:
        return f"<MetadataPreset id={self.id} slug={self.slug!r} name={self.name!r}>"
