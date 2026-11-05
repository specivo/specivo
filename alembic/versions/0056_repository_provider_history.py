"""Record the provider on each repository delivery and pull request.

Create Date: 2026-11-05
Revision ID: 0056
Revises: 0055

An integration's provider can now change. Deliveries and pull requests keep
the provider they came from, so the delivery log labels old rows with the
forge that sent them, and pull request numbers from the new forge never
overwrite the old forge's requests with the same number: the unique key of
``pull_requests`` becomes (integration, provider, number). Existing rows take
their integration's provider.
"""

import sqlalchemy as sa

from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None

_PROVIDERS = "provider IN ('github', 'gitlab', 'gitea', 'forgejo')"


def _add_provider(table: str) -> None:
    op.add_column(table, sa.Column("provider", sa.String(20), nullable=True))
    op.execute(
        f"UPDATE {table} AS t SET provider = i.provider "  # noqa: S608 - fixed table names
        "FROM repository_integrations AS i WHERE i.id = t.integration_id"
    )
    op.alter_column(table, "provider", nullable=False)
    op.create_check_constraint(f"ck_{table}_provider", table, _PROVIDERS)


def upgrade() -> None:
    _add_provider("repository_deliveries")
    _add_provider("pull_requests")
    op.drop_constraint("uq_pull_requests_integration_number", "pull_requests", type_="unique")
    op.create_unique_constraint(
        "uq_pull_requests_integration_provider_number", "pull_requests", ["integration_id", "provider", "number"]
    )


def downgrade() -> None:
    # Requests of an earlier provider that share a number with a later one cannot fit the old key.
    op.execute(
        "DELETE FROM pull_requests AS p USING repository_integrations AS i "
        "WHERE i.id = p.integration_id AND p.provider <> i.provider"
    )
    op.drop_constraint("uq_pull_requests_integration_provider_number", "pull_requests", type_="unique")
    op.create_unique_constraint("uq_pull_requests_integration_number", "pull_requests", ["integration_id", "number"])
    for table in ("pull_requests", "repository_deliveries"):
        op.drop_constraint(f"ck_{table}_provider", table, type_="check")
        op.drop_column(table, "provider")
