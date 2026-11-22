"""List the Automation role after the other custom roles.

Create Date: 2026-11-22
Revision ID: 0073
Revises: 0072

On a fresh database 0046 creates the Automation role before the seed adds
the default roles, so it gets position 1, ties with Manager and, having the
lower id, is the first role administrators see. This moves the role with
``system_key = 'automation'`` to one past the highest position of the other
custom roles. The seed does the same on a fresh database from now on.

The downgrade changes nothing: the previous position was not recorded, and
the order of roles is only presentation.
"""

from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE roles
        SET position = (
            SELECT COALESCE(MAX(other.position), 0) + 1
            FROM roles AS other
            WHERE other.builtin = 0 AND other.system_key IS NULL
        )
        WHERE system_key = 'automation' AND builtin = 0
        """
    )


def downgrade() -> None:
    pass
