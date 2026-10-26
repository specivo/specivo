"""Role visibility for users and time entries; private-by-default trackers.

Create Date: 2026-10-26
Revision ID: 0033
Revises: 0032

Three columns, each matching one Redmine setting:

- ``roles.users_visibility``: ``'all'`` or ``'members_of_visible_projects'`` —
  whether the role lists every active user or only the members of projects
  the user can reach;
- ``roles.time_entries_visibility``: ``'all'`` or ``'own'`` — whether the role
  reads every time entry in a project or only the user's own;
- ``trackers.private_by_default``: new issues of the tracker start private.

Every existing row takes the value that reproduces today's behaviour (``'all'``,
``'all'``, ``false``), so the upgrade changes nothing until an administrator
edits a role or a tracker. Adding a column with a constant default rewrites no
row, so ``trg_protect_builtin_roles`` is not involved.

Downgrade drops the three columns and their constraints.
"""

import sqlalchemy as sa

from alembic import op

revision = "0033"
down_revision = "0032"


def upgrade() -> None:
    op.add_column(
        "roles",
        sa.Column("users_visibility", sa.String(30), nullable=False, server_default="all"),
    )
    op.add_column(
        "roles",
        sa.Column("time_entries_visibility", sa.String(30), nullable=False, server_default="all"),
    )
    op.create_check_constraint(
        "ck_roles_users_visibility",
        "roles",
        "users_visibility IN ('all', 'members_of_visible_projects')",
    )
    op.create_check_constraint(
        "ck_roles_time_entries_visibility",
        "roles",
        "time_entries_visibility IN ('all', 'own')",
    )
    op.add_column(
        "trackers",
        sa.Column("private_by_default", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("trackers", "private_by_default")
    op.drop_constraint("ck_roles_time_entries_visibility", "roles", type_="check")
    op.drop_constraint("ck_roles_users_visibility", "roles", type_="check")
    op.drop_column("roles", "time_entries_visibility")
    op.drop_column("roles", "users_visibility")
