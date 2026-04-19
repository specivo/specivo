"""Widen project key columns from varchar(12) to varchar(128).

Create Date: 2026-04-19
Revision ID: 0020
Revises: 0019

Raises the max project key length to 128 characters. Also updates the
CHECK constraint on projects.key to allow up to 128 chars.
"""

import sqlalchemy as sa

from alembic import op

revision = "0020"
down_revision = "0019"


def upgrade() -> None:
    # Widen varchar columns
    op.alter_column("projects", "key", type_=sa.String(128), existing_type=sa.String(12))
    op.alter_column("project_key_aliases", "old_key", type_=sa.String(128), existing_type=sa.String(12))
    op.alter_column("issues", "project_key", type_=sa.String(128), existing_type=sa.String(12))

    # Replace CHECK constraint with wider regex
    op.drop_constraint("ck_projects_key_format", "projects", type_="check")
    op.create_check_constraint(
        "ck_projects_key_format",
        "projects",
        "key ~ '^[A-Z][A-Z0-9]{1,127}$'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_projects_key_format", "projects", type_="check")
    op.create_check_constraint(
        "ck_projects_key_format",
        "projects",
        "key ~ '^[A-Z][A-Z0-9]{1,9}$'",
    )

    op.alter_column("issues", "project_key", type_=sa.String(12), existing_type=sa.String(128))
    op.alter_column("project_key_aliases", "old_key", type_=sa.String(12), existing_type=sa.String(128))
    op.alter_column("projects", "key", type_=sa.String(12), existing_type=sa.String(128))
