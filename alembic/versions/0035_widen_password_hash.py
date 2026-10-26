"""Widen users.password_hash to 255 characters.

Create Date: 2026-10-26
Revision ID: 0035
Revises: 0034

``users.password_hash`` now holds either a native bcrypt hash or a hash
imported from another system under an ``<algorithm>$`` prefix, verified by a
legacy hasher until the account's first sign-in replaces it. The wrapped
Redmine form is 106 characters and fits the old width, but another source's
format (a long PBKDF2 or argon2 parameter string) may not, and a hasher plugin
should never need a schema change. Widening a varchar on PostgreSQL rewrites
nothing.
"""

from alembic import op

revision = "0035"
down_revision = "0034"


def upgrade() -> None:
    op.execute("ALTER TABLE users ALTER COLUMN password_hash TYPE varchar(255)")


def downgrade() -> None:
    # A legacy value longer than the old width cannot be kept. Such an account
    # loses its imported password and has to use password recovery, which is
    # the state it would be in had the import not carried the password over.
    op.execute("UPDATE users SET password_hash = NULL WHERE length(password_hash) > 128")
    op.execute("ALTER TABLE users ALTER COLUMN password_hash TYPE varchar(128)")
