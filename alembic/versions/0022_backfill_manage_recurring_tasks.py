"""Backfill manage_recurring_tasks permission on roles that have manage_versions.

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-17

Grants recurring-task management to any role that currently grants
``manage_versions`` (the same roles that plan releases are expected to own
recurring schedules). The wildcard ``*`` already grants both, so wildcard
roles are skipped.

Idempotent: appends ``manage_recurring_tasks`` only if not already present.
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Append "manage_recurring_tasks" to any role that already has
    # "manage_versions" but does not yet have "manage_recurring_tasks".
    # Wildcard ("*") roles are skipped — they already implicitly grant the
    # new permission.
    op.execute(
        """
        UPDATE roles
           SET permissions = permissions || '["manage_recurring_tasks"]'::jsonb
         WHERE permissions @> '["manage_versions"]'::jsonb
           AND NOT (permissions @> '["manage_recurring_tasks"]'::jsonb)
           AND NOT (permissions @> '["*"]'::jsonb)
        """
    )


def downgrade() -> None:
    # Remove "manage_recurring_tasks" from every role's permissions array.
    op.execute(
        """
        UPDATE roles
           SET permissions = (
               SELECT COALESCE(jsonb_agg(elem), '[]'::jsonb)
                 FROM jsonb_array_elements(permissions) AS elem
                WHERE elem <> '"manage_recurring_tasks"'::jsonb
           )
         WHERE permissions @> '["manage_recurring_tasks"]'::jsonb
        """
    )
