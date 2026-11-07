"""AI usage entries accept the attribution methods ``agent_log`` and ``manual``.

Create Date: 2026-11-07
Revision ID: 0066
Revises: 0065

- ``agent_log``: the issue a usage reporter read from the agent's own Specivo
  MCP calls in its transcript.
- ``manual``: a person moved the entry in Specivo (web, API or MCP). Replays
  of the bucket by a reporter never change such an entry.

Only the check constraint changes. Entries moved by hand before this revision
keep ``explicit``: earlier moves cannot be told apart from explicit hints.
The downgrade maps both new values back (``agent_log`` to ``explicit``,
``manual`` to ``explicit`` or ``none``) and restores the old constraint.
"""

from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None

_OLD = ("explicit", "env", "mcp_session", "commit", "branch", "repo", "none")
_NEW = ("explicit", "env", "agent_log", "mcp_session", "commit", "branch", "repo", "manual", "none")
_NAME = "ck_ai_usage_entries_attribution_method"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.drop_constraint(_NAME, "ai_usage_entries", type_="check")
    op.create_check_constraint(_NAME, "ai_usage_entries", _in("attribution_method", _NEW))


def downgrade() -> None:
    op.execute(
        """
        UPDATE ai_usage_entries
        SET attribution_method = CASE WHEN project_id IS NULL THEN 'none' ELSE 'explicit' END
        WHERE attribution_method IN ('agent_log', 'manual')
        """
    )
    op.drop_constraint(_NAME, "ai_usage_entries", type_="check")
    op.create_check_constraint(_NAME, "ai_usage_entries", _in("attribution_method", _OLD))
