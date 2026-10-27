"""Give refresh tokens a stable session id and the columns SSO sessions need.

Create Date: 2026-10-27
Revision ID: 0042
Revises: 0041

The refresh token is the session, but rotation replaces the row, so until now a
session had no identity that survived a refresh. ``session_id`` is copied onto
every rotated row and is the ``sid`` claim of the access tokens the session
mints, which is what lets a revocation cut those tokens off immediately.

- ``session_id``: UUID, NOT NULL. ``gen_random_uuid()`` is volatile, so adding
  the column with it as the default gives every existing row its own session.
- ``auth_method``: ``password`` or ``sso`` (``ck_refresh_tokens_auth_method``).
- ``user_identity_id``: the external identity an SSO session signed in with;
  ``SET NULL`` when the identity is unlinked (its sessions are revoked first).
- ``idp_sid``: the IdP's session id, for back-channel logout.
- ``id_token_encrypted``: the sealed ID token, sent as ``id_token_hint``.
- ``absolute_expires_at``: a hard cap after which the session cannot refresh.

Indexes: ``session_id`` (revocation by session) and a partial
``(user_identity_id, idp_sid)`` (back-channel logout, unlink; it also serves
the foreign key).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0042"
down_revision = "0041"


def upgrade() -> None:
    op.add_column(
        "refresh_tokens",
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
    )
    op.add_column(
        "refresh_tokens",
        sa.Column("auth_method", sa.String(length=20), server_default="password", nullable=False),
    )
    op.add_column(
        "refresh_tokens",
        sa.Column(
            "user_identity_id",
            sa.Integer(),
            sa.ForeignKey("user_identities.id", ondelete="SET NULL", name="fk_refresh_tokens_user_identity_id"),
            nullable=True,
        ),
    )
    op.add_column("refresh_tokens", sa.Column("idp_sid", sa.String(length=255), nullable=True))
    op.add_column("refresh_tokens", sa.Column("id_token_encrypted", sa.Text(), nullable=True))
    op.add_column("refresh_tokens", sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=True))

    op.create_check_constraint(
        "ck_refresh_tokens_auth_method",
        "refresh_tokens",
        "auth_method IN ('password', 'sso')",
    )
    op.create_index("ix_refresh_tokens_session_id", "refresh_tokens", ["session_id"])
    op.create_index(
        "ix_refresh_tokens_user_identity_idp_sid",
        "refresh_tokens",
        ["user_identity_id", "idp_sid"],
        postgresql_where=sa.text("user_identity_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_user_identity_idp_sid", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_session_id", table_name="refresh_tokens")
    op.drop_constraint("ck_refresh_tokens_auth_method", "refresh_tokens", type_="check")
    op.drop_column("refresh_tokens", "absolute_expires_at")
    op.drop_column("refresh_tokens", "id_token_encrypted")
    op.drop_column("refresh_tokens", "idp_sid")
    op.drop_column("refresh_tokens", "user_identity_id")
    op.drop_column("refresh_tokens", "auth_method")
    op.drop_column("refresh_tokens", "session_id")
