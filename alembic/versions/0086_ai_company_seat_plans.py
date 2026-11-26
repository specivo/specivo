"""Company seat plans: subscriptions without a holder, with a seat count and dated members.

Create Date: 2026-11-23
Revision ID: 0086
Revises: 0085

A company Team or Enterprise plan covers many people and is paid by the
employer. It is an ``ai_subscriptions`` row without a holder, the fee schedule
of an instance billing account (kind ``subscription``, payer ``employer``).

- ``ai_subscriptions.user_id`` becomes nullable: ``NULL`` is a company plan.
- ``ai_subscriptions.seat_count`` (nullable integer): set exactly when there is
  no holder, at least 1 (CHECK ``ck_ai_subscriptions_company_seats``).
- The fee CHECK ``ck_ai_subscriptions_amount_positive`` allows zero for a
  company plan; a personal plan's fee stays above zero.
- New table ``ai_subscription_members``: ``subscription_id`` (FK ON DELETE
  CASCADE), ``user_id`` (FK ON DELETE CASCADE), ``starts_on`` and ``ends_on``
  (UTC days, both inclusive, ``ends_on`` NULL = open, CHECK on their order),
  ``created_by_id`` (FK ON DELETE SET NULL) and timestamps; indexes on
  ``(subscription_id, starts_on)``, ``(user_id, starts_on)`` and
  ``created_by_id``.

Existing subscriptions are personal and stay as they are. The downgrade
deletes the company plans (usage entries keep their billing account and lose
the plan link, ``ON DELETE SET NULL``) and their members, then restores the
old columns and checks.
"""

import sqlalchemy as sa

from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("ai_subscriptions", "user_id", existing_type=sa.Integer(), nullable=True)
    op.add_column("ai_subscriptions", sa.Column("seat_count", sa.Integer(), nullable=True))
    op.drop_constraint("ck_ai_subscriptions_amount_positive", "ai_subscriptions", type_="check")
    op.create_check_constraint(
        "ck_ai_subscriptions_amount_positive",
        "ai_subscriptions",
        "amount > 0 OR (user_id IS NULL AND amount >= 0)",
    )
    op.create_check_constraint(
        "ck_ai_subscriptions_company_seats",
        "ai_subscriptions",
        "(user_id IS NULL) = (seat_count IS NOT NULL) AND (seat_count IS NULL OR seat_count >= 1)",
    )

    op.create_table(
        "ai_subscription_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("ai_subscriptions.id", ondelete="CASCADE", name="fk_ai_subscription_members_subscription_id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_ai_subscription_members_user_id"),
            nullable=False,
        ),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column(
            "created_by_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_ai_subscription_members_created_by_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("ends_on IS NULL OR ends_on >= starts_on", name="ck_ai_subscription_members_period"),
    )
    op.create_index(
        "ix_ai_subscription_members_subscription", "ai_subscription_members", ["subscription_id", "starts_on"]
    )
    op.create_index("ix_ai_subscription_members_user", "ai_subscription_members", ["user_id", "starts_on"])
    op.create_index("ix_ai_subscription_members_created_by_id", "ai_subscription_members", ["created_by_id"])


def downgrade() -> None:
    op.drop_index("ix_ai_subscription_members_created_by_id", table_name="ai_subscription_members")
    op.drop_index("ix_ai_subscription_members_user", table_name="ai_subscription_members")
    op.drop_index("ix_ai_subscription_members_subscription", table_name="ai_subscription_members")
    op.drop_table("ai_subscription_members")
    op.execute("DELETE FROM ai_subscriptions WHERE user_id IS NULL")
    op.drop_constraint("ck_ai_subscriptions_company_seats", "ai_subscriptions", type_="check")
    op.drop_constraint("ck_ai_subscriptions_amount_positive", "ai_subscriptions", type_="check")
    op.create_check_constraint("ck_ai_subscriptions_amount_positive", "ai_subscriptions", "amount > 0")
    op.drop_column("ai_subscriptions", "seat_count")
    op.alter_column("ai_subscriptions", "user_id", existing_type=sa.Integer(), nullable=False)
