"""Agents work for a person: ``users.works_for_id``.

Create Date: 2026-11-25
Revision ID: 0091
Revises: 0090

- ``users.works_for_id``: the person a service account works for, ``ON DELETE
  SET NULL``. The AI usage such an agent reports is billed through that
  person's billing accounts and plans. Only a service account may have one,
  and never itself (``ck_users_works_for_agent``); a partial index serves the
  lookups of a person's agents.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("works_for_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_users_works_for_id", "users", "users", ["works_for_id"], ["id"], ondelete="SET NULL")
    op.create_check_constraint(
        "ck_users_works_for_agent", "users", "works_for_id IS NULL OR (is_service_account AND works_for_id <> id)"
    )
    op.create_index(
        "ix_users_works_for_id", "users", ["works_for_id"], postgresql_where=sa.text("works_for_id IS NOT NULL")
    )


def downgrade() -> None:
    op.drop_index("ix_users_works_for_id", table_name="users")
    op.drop_constraint("ck_users_works_for_agent", "users", type_="check")
    op.drop_constraint("fk_users_works_for_id", "users", type_="foreignkey")
    op.drop_column("users", "works_for_id")
