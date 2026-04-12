"""Add content_type discriminator to metadata_schemas.

Revision ID: 0018
Revises: 0017
Create Date: 2026-04-12

Adds a ``content_type`` column to ``metadata_schemas`` so the same table
can describe metadata for multiple content kinds (issues, and in the
future, wiki pages, sprints, etc.). The existing unique constraint on
(project_id, tracker_id, name) is extended to include ``content_type``.
"""

import sqlalchemy as sa

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "metadata_schemas",
        sa.Column(
            "content_type",
            sa.String(32),
            nullable=False,
            server_default="issue",
        ),
    )
    op.drop_constraint(
        "uq_metadata_schema_project_tracker_name",
        "metadata_schemas",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_metadata_schema_project_tracker_name",
        "metadata_schemas",
        ["project_id", "tracker_id", "name", "content_type"],
    )
    op.create_index(
        "ix_metadata_schemas_content_type",
        "metadata_schemas",
        ["content_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_metadata_schemas_content_type", table_name="metadata_schemas")
    op.drop_constraint(
        "uq_metadata_schema_project_tracker_name",
        "metadata_schemas",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_metadata_schema_project_tracker_name",
        "metadata_schemas",
        ["project_id", "tracker_id", "name"],
    )
    op.drop_column("metadata_schemas", "content_type")
