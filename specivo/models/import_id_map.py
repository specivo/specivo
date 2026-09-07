"""Mapping from a source-system identifier to the Specivo row it became.

Written by :class:`specivo.importers.core.id_map.ImportIdMap` during an import
and read by every loader before it creates anything, which is what lets an
interrupted import be re-run safely.

Rows are immutable once written, so there is no ``updated_at``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base


class ImportIdMapping(Base):
    """One source entity and the Specivo row created for it.

    ``source_system`` plus ``source_instance`` scope the mapping, so one Specivo
    database can absorb several trackers — or two separate installations of the
    same tracker — without their identifiers colliding.
    """

    __tablename__ = "import_id_map"

    __table_args__ = (
        # Identity of a source entity. Note the absence of import_run_id: a
        # resumed run gets a new id but must still see the earlier run's rows.
        UniqueConstraint(
            "source_system",
            "source_instance",
            "entity_type",
            "source_id",
            name="uq_import_id_map_source",
        ),
        Index("ix_import_id_map_run", "import_run_id"),
        Index("ix_import_id_map_target", "target_table", "target_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Groups the rows written by one invocation. Reporting and audit only.
    import_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # e.g. "redmine"; later "rt", "jira".
    source_system: Mapped[str] = mapped_column(String(30), nullable=False)
    # Distinguishes two installations of the same system, e.g. a host name.
    source_instance: Mapped[str] = mapped_column(String(255), nullable=False)
    # Value from specivo.importers.core.ir.EntityType.
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # The source's own primary key, stringified.
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    target_table: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<ImportIdMapping {self.source_system}:{self.entity_type}"
            f" {self.source_id} -> {self.target_table}.{self.target_id}>"
        )
