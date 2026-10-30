"""Merge automation acts only on links that were closing when the pull request merged.

Create Date: 2026-10-30
Revision ID: 0047
Revises: 0046

``issue_pull_request_links.merge_qualified_at`` is set on every closing link
at the moment the stored pull request becomes merged. Merge automation acts
only on links that carry it, so a reference or fixing keyword added by
editing the text after the merge is recorded but never moves an issue.

Existing links of merged pull requests that automation already acted on or
refused are marked qualified, so a refused move is still retried.
"""

import sqlalchemy as sa

from alembic import op

revision = "0047"
down_revision = "0046"


def upgrade() -> None:
    op.add_column(
        "issue_pull_request_links",
        sa.Column("merge_qualified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE issue_pull_request_links
        SET merge_qualified_at = COALESCE(merge_handled_at, merge_refused_at)
        WHERE closing AND (merge_handled_at IS NOT NULL OR merge_refused_at IS NOT NULL)
        """
    )


def downgrade() -> None:
    op.drop_column("issue_pull_request_links", "merge_qualified_at")
