"""Record when a project's link graph was last fully rebuilt.

Create Date: 2026-11-21
Revision ID: 0071
Revises: 0070

- ``projects.links_rebuilt_at`` (timestamptz, nullable) is added. Every full
  link graph rebuild of the project (or of the whole instance) sets it when
  it finishes, and a new project gets it on creation, since it has nothing to
  index. Existing rows stay ``NULL``: a periodic task queues a full rebuild
  for every project where it is ``NULL``, which covers upgraded instances and
  restored dumps.

The downgrade drops the column.
"""

import sqlalchemy as sa

from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("links_rebuilt_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "links_rebuilt_at")
