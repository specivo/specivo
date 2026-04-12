"""Backfill manage_sprints permission on roles that already have manage_versions.

Revision ID: 0019
Revises: 0018
Create Date: 2026-04-12

Splits sprint management out of ``manage_versions``. Any role that currently
grants ``manage_versions`` is assumed to also be expected to manage sprints
(this matches the previous behavior where sprint endpoints piggybacked on
``manage_versions``). The wildcard ``*`` already grants both, so wildcard
roles are skipped.

Idempotent: appends ``manage_sprints`` only if not already present.
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Append "manage_sprints" to any role that already has "manage_versions"
    # but does not yet have "manage_sprints". Wildcard ("*") roles are
    # skipped — they already implicitly grant the new permission.
    op.execute(
        """
        UPDATE roles
           SET permissions = permissions || '["manage_sprints"]'::jsonb
         WHERE permissions @> '["manage_versions"]'::jsonb
           AND NOT (permissions @> '["manage_sprints"]'::jsonb)
           AND NOT (permissions @> '["*"]'::jsonb)
        """
    )


def downgrade() -> None:
    # Remove "manage_sprints" from every role's permissions array.
    op.execute(
        """
        UPDATE roles
           SET permissions = (
               SELECT COALESCE(jsonb_agg(elem), '[]'::jsonb)
                 FROM jsonb_array_elements(permissions) AS elem
                WHERE elem <> '"manage_sprints"'::jsonb
           )
         WHERE permissions @> '["manage_sprints"]'::jsonb
        """
    )
