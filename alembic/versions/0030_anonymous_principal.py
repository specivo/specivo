"""Reserve a users row for the anonymous principal.

Create Date: 2026-09-15
Revision ID: 0030
Revises: 0029

A visitor without an account is represented by one real ``users`` row rather
than by a missing user. Code that takes a ``User`` keeps taking one, and
nothing has to learn that ``None`` might mean "somebody who is not signed in"
— a meaning that would collide with nullable user columns such as
``issues.assigned_to_id``.

A row that stands for nobody in particular has to be unable to act as anyone,
so the database enforces that rather than every code path:

- ``is_anonymous`` marks the row, and ``uq_users_single_anonymous`` (a partial
  unique index) allows exactly one such row.
- ``ck_users_anonymous_inert`` pins it deactivated, not an administrator, not a
  service account, without a password and without a forced password change.
- ``reject_anonymous_principal()`` is a trigger on ``members`` and
  ``user_group_members`` that refuses any row naming it. Access for anonymous
  visitors is never granted through a membership.

The row uses the login ``$anonymous``. User creation only accepts logins made
of lowercase letters, digits, ``_`` and ``-``, so no account created through
the application can collide with it. An existing account that already uses the
login or the ``anonymous@specivo.invalid`` address (possible only through
direct database edits) stops the upgrade with an explanation instead of
failing on a unique index.

The row carries no permissions and nothing reads it yet; this migration
changes no behaviour for existing users.

Downgrade removes the row. Nothing references it at this revision.
"""

import sqlalchemy as sa

from alembic import op

revision = "0030"
down_revision = "0029"


def upgrade() -> None:
    clash = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id, login, email FROM users "
                "WHERE lower(login) = '$anonymous' OR lower(email) = 'anonymous@specivo.invalid'"
            )
        )
        .first()
    )
    if clash is not None:
        raise RuntimeError(
            f"User id={clash.id} (login {clash.login!r}, email {clash.email!r}) uses the login or email "
            "reserved for the anonymous user. Rename that account and run the upgrade again."
        )

    op.execute("ALTER TABLE users ADD COLUMN is_anonymous boolean NOT NULL DEFAULT false")
    op.execute("CREATE UNIQUE INDEX uq_users_single_anonymous ON users (is_anonymous) WHERE is_anonymous")
    op.execute(
        """
        ALTER TABLE users
            ADD CONSTRAINT ck_users_anonymous_inert
            CHECK (
                NOT is_anonymous
                OR (
                    status = 'deactivated'
                    AND NOT is_admin
                    AND NOT is_service_account
                    AND password_hash IS NULL
                    AND NOT must_change_password
                )
            )
        """
    )

    op.execute(
        """
        CREATE FUNCTION reject_anonymous_principal() RETURNS trigger AS $$
        BEGIN
            IF NEW.user_id IS NOT NULL
               AND EXISTS (SELECT 1 FROM users WHERE id = NEW.user_id AND is_anonymous) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'check_violation',
                    CONSTRAINT = 'reject_anonymous_principal',
                    MESSAGE = 'the anonymous user cannot be added to ' || TG_TABLE_NAME;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_members_reject_anonymous
            BEFORE INSERT OR UPDATE ON members
            FOR EACH ROW EXECUTE FUNCTION reject_anonymous_principal()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_user_group_members_reject_anonymous
            BEFORE INSERT OR UPDATE ON user_group_members
            FOR EACH ROW EXECUTE FUNCTION reject_anonymous_principal()
        """
    )

    op.execute(
        """
        INSERT INTO users (
            login, email, display_name, status, password_hash,
            is_admin, is_service_account, must_change_password, is_anonymous
        ) VALUES (
            '$anonymous', 'anonymous@specivo.invalid', 'Anonymous', 'deactivated', NULL,
            false, false, false, true
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_user_group_members_reject_anonymous ON user_group_members")
    op.execute("DROP TRIGGER trg_members_reject_anonymous ON members")
    op.execute("DROP FUNCTION reject_anonymous_principal()")
    op.execute("DELETE FROM users WHERE is_anonymous")
    op.execute("ALTER TABLE users DROP CONSTRAINT ck_users_anonymous_inert")
    op.execute("DROP INDEX uq_users_single_anonymous")
    op.execute("ALTER TABLE users DROP COLUMN is_anonymous")
