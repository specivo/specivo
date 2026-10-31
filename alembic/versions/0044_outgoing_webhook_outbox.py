"""Turn webhook deliveries into an outbox and move webhook secrets to secret_box.

Create Date: 2026-10-30
Revision ID: 0044
Revises: 0043

``webhook_deliveries`` holds one row per delivery instead of one per attempt:

* ``delivery_uuid`` UUID NOT NULL, unique: sent as ``X-Specivo-Delivery`` on
  every attempt, so a receiver can drop retries it has already processed.
* ``status`` VARCHAR(20) NOT NULL (``pending``, ``succeeded``, ``failed``),
  backfilled from the old ``success`` flag, which is dropped.
* ``next_attempt_at`` TIMESTAMPTZ: when a pending row is due; the sweeper
  re-queues pending rows past it. A partial index covers that query.
* ``last_error`` TEXT: why the latest attempt failed.

``webhooks`` gains ``include_private`` (default false): only a webhook with it
on receives private issues and private notes. ``webhooks.secret`` becomes TEXT
and is re-encrypted from the old ad-hoc key (Fernet keyed with SHA-256 of
``SECRET_KEY``) to ``secret_box`` purpose ``webhook-secret``. A value the old
key cannot read was stored before encryption existed and is taken as plaintext.
The downgrade encrypts the secrets back with the old key.
"""

import base64
import hashlib

import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.dialects import postgresql

from alembic import op
from specivo.core import secret_box
from specivo.core.config import get_settings

revision = "0044"
down_revision = "0043"

_PURPOSE = "webhook-secret"


def _legacy_fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(get_settings().secret_key.encode()).digest())
    return Fernet(key)


def _legacy_decrypt(value: str) -> str:
    try:
        return _legacy_fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, UnicodeError):
        return value


def _rewrite_secrets(convert) -> None:  # type: ignore[no-untyped-def]
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, secret FROM webhooks")).all()
    for row in rows:
        conn.execute(
            sa.text("UPDATE webhooks SET secret = :secret WHERE id = :id"),
            {"id": row.id, "secret": convert(row.secret)},
        )


def upgrade() -> None:
    op.alter_column("webhooks", "secret", type_=sa.Text(), existing_nullable=False)
    op.add_column(
        "webhooks",
        sa.Column("include_private", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    _rewrite_secrets(lambda value: secret_box.encrypt(_legacy_decrypt(value), _PURPOSE))

    op.add_column("webhook_deliveries", sa.Column("delivery_uuid", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("webhook_deliveries", sa.Column("status", sa.String(length=20), nullable=True))
    op.add_column("webhook_deliveries", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("webhook_deliveries", sa.Column("last_error", sa.Text(), nullable=True))
    op.execute(
        "UPDATE webhook_deliveries SET delivery_uuid = gen_random_uuid(), "
        "status = CASE WHEN success THEN 'succeeded' ELSE 'failed' END"
    )
    op.alter_column("webhook_deliveries", "delivery_uuid", nullable=False)
    op.alter_column("webhook_deliveries", "status", nullable=False, server_default="pending")
    op.alter_column("webhook_deliveries", "attempts", server_default="0")
    op.drop_column("webhook_deliveries", "success")
    op.create_unique_constraint("uq_webhook_deliveries_delivery_uuid", "webhook_deliveries", ["delivery_uuid"])
    op.create_check_constraint(
        "ck_webhook_deliveries_status",
        "webhook_deliveries",
        "status IN ('pending', 'succeeded', 'failed')",
    )
    op.create_index(
        "ix_webhook_deliveries_pending_next_attempt",
        "webhook_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_webhook_deliveries_webhook_id_created_at",
        "webhook_deliveries",
        ["webhook_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_webhook_id_created_at", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_pending_next_attempt", table_name="webhook_deliveries")
    op.drop_constraint("ck_webhook_deliveries_status", "webhook_deliveries", type_="check")
    op.drop_constraint("uq_webhook_deliveries_delivery_uuid", "webhook_deliveries", type_="unique")
    op.add_column(
        "webhook_deliveries",
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute("UPDATE webhook_deliveries SET success = (status = 'succeeded')")
    op.alter_column("webhook_deliveries", "attempts", server_default=None)
    op.drop_column("webhook_deliveries", "last_error")
    op.drop_column("webhook_deliveries", "next_attempt_at")
    op.drop_column("webhook_deliveries", "status")
    op.drop_column("webhook_deliveries", "delivery_uuid")

    def _to_legacy(value: str) -> str:
        try:
            plain = secret_box.decrypt(value, _PURPOSE)
        except secret_box.SecretUnreadableError:
            # Unreadable with this SECRET_KEY: keep the token, the webhook needs a new secret either way.
            return value
        return _legacy_fernet().encrypt(plain.encode()).decode()

    _rewrite_secrets(_to_legacy)
    op.drop_column("webhooks", "include_private")
    op.alter_column("webhooks", "secret", type_=sa.String(length=255), existing_nullable=False)
