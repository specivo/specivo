"""Let an SSO provider deactivate the account of a user it refuses.

Create Date: 2026-10-27
Revision ID: 0043
Revises: 0042

``sso_providers.deactivate_on_access_loss``: BOOLEAN NOT NULL, default false.
When on, a sign-in refused by the provider's access policy (email domain,
required groups, group overage) also deactivates the Specivo account that
identity belongs to, which ends its sessions and deactivates its API keys.
Off keeps the earlier behaviour: only the identity's sessions are revoked.
"""

import sqlalchemy as sa

from alembic import op

revision = "0043"
down_revision = "0042"


def upgrade() -> None:
    op.add_column(
        "sso_providers",
        sa.Column("deactivate_on_access_loss", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("sso_providers", "deactivate_on_access_loss")
