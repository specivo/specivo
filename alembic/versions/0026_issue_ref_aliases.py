"""Issue reference aliases for cross-project move.

Create Date: 2026-06-24
Revision ID: 0026
Revises: 0025

Adds ``issue_ref_aliases`` so an issue's previous ``KEY-N`` reference keeps
resolving after it is moved to another project (the move assigns a new
project_key + sequence_number). Mirrors ``project_key_aliases`` for project
key renames.
"""

from alembic import op

revision = "0026"
down_revision = "0025"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE issue_ref_aliases (
            id serial PRIMARY KEY,
            old_project_key varchar(128) NOT NULL,
            old_sequence_number integer NOT NULL,
            issue_id integer NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_issue_ref_aliases_old_ref UNIQUE (old_project_key, old_sequence_number)
        )
        """
    )
    op.execute("CREATE INDEX ix_issue_ref_aliases_issue_id ON issue_ref_aliases (issue_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS issue_ref_aliases")
