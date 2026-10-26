"""Create keyword-search chunks for existing comments.

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-26

Comment search, keyword included, reads ``search_chunks``. Until now a comment
only got a chunk from the embedding backfill command, so on most installs no
comment could be found. Comments now get their chunk as they are written; this
creates the missing ones for comments that already exist.

A comment's chunk is its whole text, so it is built here in SQL, honouring the
comment settings the application applies: ``SEARCH_INDEX_COMMENTS``,
``SEARCH_MIN_COMMENT_LENGTH`` and ``SEARCH_EXCLUDE_BOT_COMMENTS`` (comments made
through an API key or by a service account). Only comments on issues are
indexed, as comment search only covers those. Comments that already have a
chunk are left alone. The full-text vectors are filled by the existing
``trg_search_chunks_search_vector`` trigger. No embeddings are made here; the
backfill command or the worker adds them when a model is installed.

Downgrade is a no-op: the chunks are what the application would have written,
and the previous revision works with them.
"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def _comment_settings() -> tuple[bool, int, bool]:
    try:
        from specivo.core.config import get_settings

        settings = get_settings()
        return (
            settings.search_index_comments,
            settings.search_min_comment_length,
            settings.search_exclude_bot_comments,
        )
    except Exception:
        return True, 20, True


def upgrade() -> None:
    create_comment_chunks(op.get_bind(), *_comment_settings())


def create_comment_chunks(bind: Connection, index_comments: bool, min_length: int, exclude_bots: bool) -> None:
    """Create the missing chunks for existing comments, following the comment settings."""
    if not index_comments:
        return

    bot_filter = "AND j.api_key_id IS NULL AND NOT COALESCE(u.is_service_account, false)" if exclude_bots else ""

    def indexable(extra_join: str = "") -> str:
        return f"""
              FROM journals j
              JOIN issues i ON i.id = j.issue_id
              {extra_join}
              LEFT JOIN users u ON u.id = j.user_id
             WHERE j.notes IS NOT NULL
               AND j.notes <> ''
               AND char_length(j.notes) >= :min_length
               {bot_filter}
        """

    params = {"min_length": min_length}

    # Two statements, not one with a data-modifying CTE: the chunk trigger looks
    # up the source's project, and must see the source rows to do so.
    bind.execute(
        text(f"""
            INSERT INTO search_sources (source_type, entity_id, project_id)
            SELECT 'journal', j.id, i.project_id
            {indexable()}
            ON CONFLICT (source_type, entity_id) DO NOTHING
        """),
        params,
    )
    source_join = "JOIN search_sources ss ON ss.source_type = 'journal' AND ss.entity_id = j.id"
    bind.execute(
        text(f"""
            INSERT INTO search_chunks (source_id, chunk_index, content, metadata_json)
            SELECT ss.id, 0, j.notes,
                   jsonb_build_object('source_type', 'journal', 'entity_id', j.id, 'project_id', ss.project_id)
            {indexable(source_join)}
               AND NOT EXISTS (SELECT 1 FROM search_chunks sc WHERE sc.source_id = ss.id)
        """),
        params,
    )


def downgrade() -> None:
    pass
