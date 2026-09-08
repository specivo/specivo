"""User groups that can hold project memberships.

Create Date: 2026-09-08
Revision ID: 0028
Revises: 0027

Adds ``user_groups`` and ``user_group_members``, and reshapes ``members`` so a
membership is held by a principal rather than always by a user: ``user_id``
becomes nullable, a nullable ``group_id`` is added, and a CHECK requires
exactly one of the two.

Existing rows all have ``user_id`` set, so no data migration is needed.

Uniqueness per project is expressed as two plain unique constraints. PostgreSQL
treats NULLs as distinct, so the user constraint ignores group rows and vice
versa, and the CHECK guarantees no row is ever both.
"""

from alembic import op

revision = "0028"
down_revision = "0027"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE user_groups (
            id serial PRIMARY KEY,
            name varchar(255) NOT NULL,
            description text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # Case-insensitive uniqueness on the group name, as users have on login/email.
    op.execute("CREATE UNIQUE INDEX uq_user_groups_name_ci ON user_groups (lower(name))")

    op.execute(
        """
        CREATE TABLE user_group_members (
            id serial PRIMARY KEY,
            group_id integer NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
            user_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_user_group_members_group_user UNIQUE (group_id, user_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_user_group_members_group_id ON user_group_members (group_id)")
    op.execute("CREATE INDEX ix_user_group_members_user_id ON user_group_members (user_id)")

    op.execute("ALTER TABLE members ALTER COLUMN user_id DROP NOT NULL")
    op.execute("ALTER TABLE members ADD COLUMN group_id integer")
    op.execute(
        """
        ALTER TABLE members
            ADD CONSTRAINT fk_members_group_id
            FOREIGN KEY (group_id) REFERENCES user_groups(id) ON DELETE CASCADE
        """
    )
    op.execute("CREATE INDEX ix_members_group_id ON members (group_id)")
    op.execute(
        """
        ALTER TABLE members
            ADD CONSTRAINT ck_members_one_principal
            CHECK (num_nonnulls(user_id, group_id) = 1)
        """
    )
    op.execute(
        """
        ALTER TABLE members
            ADD CONSTRAINT uq_members_group_project UNIQUE (group_id, project_id)
        """
    )


def downgrade() -> None:
    # Group-held memberships cannot survive a column that no longer exists.
    op.execute("DELETE FROM members WHERE group_id IS NOT NULL")

    op.execute("ALTER TABLE members DROP CONSTRAINT IF EXISTS uq_members_group_project")
    op.execute("ALTER TABLE members DROP CONSTRAINT IF EXISTS ck_members_one_principal")
    op.execute("DROP INDEX IF EXISTS ix_members_group_id")
    op.execute("ALTER TABLE members DROP CONSTRAINT IF EXISTS fk_members_group_id")
    op.execute("ALTER TABLE members DROP COLUMN IF EXISTS group_id")
    op.execute("ALTER TABLE members ALTER COLUMN user_id SET NOT NULL")

    op.execute("DROP TABLE IF EXISTS user_group_members")
    op.execute("DROP TABLE IF EXISTS user_groups")
