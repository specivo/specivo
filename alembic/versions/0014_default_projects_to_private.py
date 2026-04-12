"""Default projects to private and set existing projects to private.

Create Date: 2026-04-12
Revision ID: 0014
Revises: 0013

Change the default value of projects.is_public from true to false.
All existing projects are set to private.
"""

from alembic import op


revision = "0014"
down_revision = "0013"


def upgrade() -> None:
    op.alter_column("projects", "is_public", server_default="false")
    op.execute("UPDATE projects SET is_public = false")


def downgrade() -> None:
    op.alter_column("projects", "is_public", server_default="true")
    op.execute("UPDATE projects SET is_public = true")
