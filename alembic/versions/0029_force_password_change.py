"""Force a password change on accounts whose password somebody else chose.

Create Date: 2026-09-09
Revision ID: 0029
Revises: 0028

Adds ``users.must_change_password``. It is set when an administrator or an
import chose the password on the account owner's behalf, and cleared the moment
the owner picks their own.

The CHECK matters as much as the column. A service account has no password: it
authenticates with an API key, cannot log in with a password, and the
change-password endpoint refuses it outright. A forced-change flag on such a row
would therefore be a dead end, locking an agent out with no visible cause, so
the combination is rejected by the database rather than left to whichever code
path remembers the rule.

Existing rows default to false, which is the state every account is already in.
"""

from alembic import op

revision = "0029"
down_revision = "0028"


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN must_change_password boolean NOT NULL DEFAULT false")
    op.execute(
        """
        ALTER TABLE users
            ADD CONSTRAINT ck_users_no_forced_change_for_service_account
            CHECK (NOT (must_change_password AND is_service_account))
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT ck_users_no_forced_change_for_service_account")
    op.execute("ALTER TABLE users DROP COLUMN must_change_password")
