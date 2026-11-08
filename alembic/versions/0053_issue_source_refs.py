"""Keep the number an imported issue had in its source system.

Create Date: 2026-11-05
Revision ID: 0053
Revises: 0052

Issues already imported get their number from ``import_id_map`` here, so they
need no new import. That table holds the source's own id, which for some
systems is an internal one, and has no label from the source: running the
bundle again replaces both. A map row whose issue was deleted is skipped.
"""

import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052"

# The same label rule as ``issue_source_ref_service.source_ref_label``.
BACKFILL_SQL = """
    INSERT INTO issue_source_refs (issue_id, source_system, ref, label, is_primary)
    SELECT m.target_id,
           m.source_system,
           m.source_id,
           CASE
               WHEN m.source_id ~ '^[0-9]+$'
                   THEN upper(left(m.source_system, 1)) || substr(m.source_system, 2) || ' #' || m.source_id
               ELSE m.source_id
           END,
           true
    FROM import_id_map m
    JOIN issues i ON i.id = m.target_id
    WHERE m.entity_type = 'issue' AND m.target_table = 'issues'
    ON CONFLICT DO NOTHING
"""


def upgrade() -> None:
    op.create_table(
        "issue_source_refs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("issue_id", sa.Integer(), sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_system", sa.String(30), nullable=False),
        sa.Column("ref", sa.String(255), nullable=False),
        sa.Column("label", sa.String(300), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("issue_id", "source_system", "ref", name="uq_issue_source_refs_issue_system_ref"),
    )
    op.create_index("ix_issue_source_refs_system_ref", "issue_source_refs", ["source_system", sa.text("lower(ref)")])
    op.create_index("ix_issue_source_refs_ref", "issue_source_refs", [sa.text("lower(ref)")])
    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    op.drop_table("issue_source_refs")
