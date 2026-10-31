"""SSO offboarding: re-check sessions at the IdP, choose what a back-channel logout does.

Create Date: 2026-10-30
Revision ID: 0050
Revises: 0049

``sso_providers`` gains:

* ``verify_session_at_idp`` (on): an SSO session is renewed only after the
  IdP accepted its refresh token, so a person disabled, deleted or signed out
  at the IdP loses the Specivo session within one access-token lifetime.
  Existing providers get it on as well. That is safe: a session whose
  provider issued no refresh token (or refused the one it issued when it was
  tried at sign-in) is renewed as before, and the sessions open at upgrade
  time carry no IdP refresh token, so nobody is signed out by the upgrade.
* ``idp_check_failure`` (``allow``): what an unreachable IdP means for a
  renewal; ``deny`` ends the session.
* ``backchannel_action`` (``revoke_session``): what a back-channel logout
  ends: the IdP session's Specivo sessions (as before), every session of the
  identity, or that and the API keys of an SSO-only account.
* ``session_recheck_status`` / ``session_recheck_status_at``: what the last
  sign-in found out about the IdP's refresh tokens, for Test connection.

``refresh_tokens`` gains the IdP refresh token (sealed with ``secret_box``)
and the time the session was last checked at the IdP.
"""

import sqlalchemy as sa

from alembic import op

revision = "0050"
down_revision = "0049"


def upgrade() -> None:
    op.add_column(
        "sso_providers",
        sa.Column("verify_session_at_idp", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column(
        "sso_providers",
        sa.Column("idp_check_failure", sa.String(length=16), server_default="allow", nullable=False),
    )
    op.add_column(
        "sso_providers",
        sa.Column("backchannel_action", sa.String(length=32), server_default="revoke_session", nullable=False),
    )
    op.add_column("sso_providers", sa.Column("session_recheck_status", sa.String(length=32), nullable=True))
    op.add_column("sso_providers", sa.Column("session_recheck_status_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_sso_providers_idp_check_failure", "sso_providers", "idp_check_failure IN ('allow', 'deny')"
    )
    op.create_check_constraint(
        "ck_sso_providers_backchannel_action",
        "sso_providers",
        "backchannel_action IN ('revoke_session', 'revoke_all_sessions', 'deprovision')",
    )
    op.create_check_constraint(
        "ck_sso_providers_session_recheck_status",
        "sso_providers",
        "session_recheck_status IS NULL OR session_recheck_status IN ('ok', 'no_refresh_token', 'refresh_refused')",
    )

    op.add_column("refresh_tokens", sa.Column("idp_refresh_token_encrypted", sa.Text(), nullable=True))
    op.add_column("refresh_tokens", sa.Column("idp_checked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("refresh_tokens", "idp_checked_at")
    op.drop_column("refresh_tokens", "idp_refresh_token_encrypted")

    op.drop_constraint("ck_sso_providers_session_recheck_status", "sso_providers", type_="check")
    op.drop_constraint("ck_sso_providers_backchannel_action", "sso_providers", type_="check")
    op.drop_constraint("ck_sso_providers_idp_check_failure", "sso_providers", type_="check")
    op.drop_column("sso_providers", "session_recheck_status_at")
    op.drop_column("sso_providers", "session_recheck_status")
    op.drop_column("sso_providers", "backchannel_action")
    op.drop_column("sso_providers", "idp_check_failure")
    op.drop_column("sso_providers", "verify_session_at_idp")
