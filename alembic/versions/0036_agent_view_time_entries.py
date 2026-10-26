"""Grant view_time_entries to the seeded Agent role.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-26

Reading time entries now requires ``view_time_entries``. The seeded Agent role
only had ``log_time``, so service accounts could log time but no longer read
it back, their own entries included. This adds the permission to it.

The role is found the way the seed finds it: a custom role (``builtin = 0``)
named "Agent". When an administrator has renamed or deleted it, nothing
happens. A role that already grants the permission, directly or through
``*``, is left as it is.

Downgrade removes ``view_time_entries`` from that role again.
"""

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE roles
           SET permissions = permissions || '["view_time_entries"]'::jsonb
         WHERE name = 'Agent'
           AND builtin = 0
           AND NOT (permissions @> '["view_time_entries"]'::jsonb)
           AND NOT (permissions @> '["*"]'::jsonb)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE roles
           SET permissions = permissions - 'view_time_entries'
         WHERE name = 'Agent'
           AND builtin = 0
           AND permissions @> '["view_time_entries"]'::jsonb
        """
    )
