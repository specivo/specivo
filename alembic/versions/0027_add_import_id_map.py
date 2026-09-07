"""Identifier mapping for imports from external trackers.

Create Date: 2026-09-07
Revision ID: 0027
Revises: 0026

Adds ``import_id_map``, which remembers that a given entity in a source system
became a given row in Specivo. It is what makes an import idempotent: every
loader looks a source id up here before creating anything, so re-running an
interrupted import skips what already landed instead of duplicating it.

The uniqueness key deliberately excludes ``import_run_id``. A resumed import
gets a fresh run id but must still recognise rows written by the earlier
attempt; the run id is kept for reporting and audit only.
"""

from alembic import op

revision = "0027"
down_revision = "0026"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE import_id_map (
            id serial PRIMARY KEY,
            import_run_id uuid NOT NULL,
            source_system varchar(30) NOT NULL,
            source_instance varchar(255) NOT NULL,
            entity_type varchar(30) NOT NULL,
            source_id varchar(255) NOT NULL,
            target_table varchar(64) NOT NULL,
            target_id integer NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_import_id_map_source
                UNIQUE (source_system, source_instance, entity_type, source_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_import_id_map_run ON import_id_map (import_run_id)")
    op.execute("CREATE INDEX ix_import_id_map_target ON import_id_map (target_table, target_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS import_id_map")
