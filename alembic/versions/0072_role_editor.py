"""Role editor: a stable key for the Automation role, optimistic locking, and wiki editing for Developer.

Create Date: 2026-11-22
Revision ID: 0072
Revises: 0071

- ``roles.system_key`` (varchar 30, nullable) marks a custom role a feature
  finds by key rather than by name, so renaming it breaks nothing. The only
  value is ``automation``: the role ``$git`` holds for merge automation,
  until now looked up by its name. ``ck_roles_system_key`` limits the values,
  ``ck_roles_system_key_custom`` keeps it off builtin roles and
  ``uq_roles_system_key`` allows one role per key. The custom role named
  ``Automation`` (created by 0046) gets the key.
- ``roles.lock_version`` (integer, default 0) for optimistic locking in the
  role editor.
- The Developer role gains ``manage_wiki`` (create and edit wiki pages), but
  only while its permissions are still exactly the set the seed created; a
  role an administrator changed is left alone. The comparison is two-way
  JSONB containment, so order and duplicates do not matter.

The downgrade removes ``manage_wiki`` only from a Developer role holding
exactly the seeded set plus ``manage_wiki``, then drops the index, the checks
and the columns.
"""

import json

import sqlalchemy as sa

from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None

# The Developer role's permissions as the seed created them before this
# revision. Hard-coded: migrations never import application code.
_SEEDED_DEVELOPER = [
    "add_issues",
    "edit_issues",
    "add_issue_notes",
    "edit_own_notes",
    "manage_issue_relations",
    "manage_subtasks",
    "view_issues",
    "view_wiki",
    "log_time",
    "view_time_entries",
    "view_ai_usage",
    "log_ai_usage",
]
_WITH_WIKI = [*_SEEDED_DEVELOPER, "manage_wiki"]

_SET_EQUALS = "permissions @> CAST(:perms AS jsonb) AND CAST(:perms AS jsonb) @> permissions"


def upgrade() -> None:
    op.add_column("roles", sa.Column("system_key", sa.String(30), nullable=True))
    op.add_column("roles", sa.Column("lock_version", sa.Integer(), nullable=False, server_default="0"))
    op.create_check_constraint("ck_roles_system_key", "roles", "system_key IS NULL OR system_key IN ('automation')")
    op.create_check_constraint("ck_roles_system_key_custom", "roles", "system_key IS NULL OR builtin = 0")
    op.create_index(
        "uq_roles_system_key",
        "roles",
        ["system_key"],
        unique=True,
        postgresql_where=sa.text("system_key IS NOT NULL"),
    )
    op.execute("UPDATE roles SET system_key = 'automation' WHERE name = 'Automation' AND builtin = 0")
    op.get_bind().execute(
        sa.text(
            "UPDATE roles SET permissions = permissions || CAST('[\"manage_wiki\"]' AS jsonb) "
            f"WHERE name = 'Developer' AND builtin = 0 AND {_SET_EQUALS}"
        ),
        {"perms": json.dumps(_SEEDED_DEVELOPER)},
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE roles SET permissions = permissions - 'manage_wiki' "
            f"WHERE name = 'Developer' AND builtin = 0 AND {_SET_EQUALS}"
        ),
        {"perms": json.dumps(_WITH_WIKI)},
    )
    op.drop_index("uq_roles_system_key", table_name="roles")
    op.drop_constraint("ck_roles_system_key_custom", "roles", type_="check")
    op.drop_constraint("ck_roles_system_key", "roles", type_="check")
    op.drop_column("roles", "lock_version")
    op.drop_column("roles", "system_key")
