"""Index issue identifiers (``KEY-42``) as single full-text lexemes.

Create Date: 2026-10-26
Revision ID: 0034
Revises: 0032

PostgreSQL's default text-search parser, shared by every built-in regconfig,
reads ``ACME-42`` as the word ``acme`` followed by the *signed integer*
``-42``. The query side normalised hyphens to spaces and looked for ``42``,
so an identifier never matched the text it appeared in, whatever the
project's FTS language. ``DO-8795`` fared worse: ``do`` is an English
stopword, which left only ``-8795``.

Every indexed text now also carries each identifier-shaped token as one
lowercase lexeme (``'acme-42'``), built by ``specivo_fts_identifiers(text)``
outside the parser and therefore independent of the per-project language.
The search service matches identifier tokens in a query against exactly that
lexeme. The tokens the parser produced before are kept, so nothing that
matched before stops matching.

The three trigger functions from 0025 are redefined to append it: issues
(subject at weight A, description at B), wiki contents (title A, text B) and
search chunks (comments and attachments).

No rows are rewritten here: a full rewrite would hold row locks on every
issue, wiki revision and chunk for the length of the upgrade. Instead, when
there is anything indexed, the migration raises the instance-wide
``fts_reindex_needed`` flag, so Administration -> Settings shows the reindex
as needed and "Reindex now" runs the existing batched job (also available
as the ``reindex_fts`` CLI). Vectors also pick the lexemes up on their next edit.
Until the reindex has run, an issue is still found by its own reference,
which the search service resolves directly; only *mentions* in older text
wait for it.
"""

from alembic import op

revision = "0034"
down_revision = "0032"


# Mirrors ``specivo.services.search_service.IDENTIFIER_PATTERN``. The bounds
# keep each lexeme far below PostgreSQL's 2 KB lexeme limit, and the
# look-arounds stop a match from starting or ending inside a longer word.
_IDENTIFIER_RE = "(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]{0,63})-([0-9]{1,18})(?![A-Za-z0-9_])"


def upgrade() -> None:
    # Each lexeme gets position 1 so ts_rank_cd can score it (a lexeme without
    # positions ranks zero). The colon is escaped for SQLAlchemy's text(),
    # which would otherwise read ``:1`` as a bind parameter.
    op.execute(f"""
        CREATE OR REPLACE FUNCTION specivo_fts_identifiers(p_text text) RETURNS tsvector AS $$
            SELECT coalesce(
                CAST(string_agg(DISTINCT quote_literal(lower(m[1] || '-' || m[2])) || '\\:1', ' ') AS tsvector),
                CAST('' AS tsvector)
            )
            FROM regexp_matches(coalesce(p_text, ''), '{_IDENTIFIER_RE}', 'g') AS m
        $$ LANGUAGE sql IMMUTABLE PARALLEL SAFE
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION issues_search_vector_update() RETURNS trigger AS $$
        DECLARE
            v_cfg regconfig := specivo_fts_config(NEW.project_id);
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector(v_cfg, replace(coalesce(NEW.subject, ''), '_', ' ')), 'A') ||
                setweight(specivo_fts_identifiers(NEW.subject), 'A') ||
                setweight(to_tsvector(v_cfg, coalesce(NEW.description, '')), 'B') ||
                setweight(specivo_fts_identifiers(NEW.description), 'B');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION wiki_contents_search_vector_update() RETURNS trigger AS $$
        DECLARE
            page_title text;
            v_project_id integer;
            v_cfg regconfig;
        BEGIN
            SELECT wp.title, w.project_id INTO page_title, v_project_id
            FROM wiki_pages wp JOIN wikis w ON w.id = wp.wiki_id
            WHERE wp.id = NEW.page_id;
            v_cfg := specivo_fts_config(v_project_id);
            NEW.search_vector :=
                setweight(to_tsvector(v_cfg, replace(coalesce(page_title, ''), '_', ' ')), 'A') ||
                setweight(specivo_fts_identifiers(page_title), 'A') ||
                setweight(to_tsvector(v_cfg, coalesce(NEW.text, '')), 'B') ||
                setweight(specivo_fts_identifiers(NEW.text), 'B');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION search_chunks_search_vector_update() RETURNS trigger AS $$
        DECLARE
            v_project_id integer;
        BEGIN
            SELECT project_id INTO v_project_id FROM search_sources WHERE id = NEW.source_id;
            NEW.search_vector :=
                to_tsvector(specivo_fts_config(v_project_id), coalesce(NEW.content, '')) ||
                specivo_fts_identifiers(NEW.content);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    # Existing vectors lack the new lexemes: ask the admin to run the reindex.
    op.execute("""
        INSERT INTO settings (key, value, created_at, updated_at)
        SELECT 'fts_reindex_needed', '1', now(), now()
        WHERE EXISTS (SELECT 1 FROM issues)
           OR EXISTS (SELECT 1 FROM wiki_contents)
           OR EXISTS (SELECT 1 FROM search_chunks)
        ON CONFLICT (key) DO UPDATE SET value = '1', updated_at = now()
    """)


def downgrade() -> None:
    # Restore the 0025 trigger functions. Vectors already carrying identifier
    # lexemes keep them until their next edit or reindex; they are harmless.
    op.execute("""
        CREATE OR REPLACE FUNCTION issues_search_vector_update() RETURNS trigger AS $$
        DECLARE
            v_cfg regconfig := specivo_fts_config(NEW.project_id);
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector(v_cfg, replace(coalesce(NEW.subject, ''), '_', ' ')), 'A') ||
                setweight(to_tsvector(v_cfg, coalesce(NEW.description, '')), 'B');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION wiki_contents_search_vector_update() RETURNS trigger AS $$
        DECLARE
            page_title text;
            v_project_id integer;
            v_cfg regconfig;
        BEGIN
            SELECT wp.title, w.project_id INTO page_title, v_project_id
            FROM wiki_pages wp JOIN wikis w ON w.id = wp.wiki_id
            WHERE wp.id = NEW.page_id;
            v_cfg := specivo_fts_config(v_project_id);
            NEW.search_vector :=
                setweight(to_tsvector(v_cfg, replace(coalesce(page_title, ''), '_', ' ')), 'A') ||
                setweight(to_tsvector(v_cfg, coalesce(NEW.text, '')), 'B');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION search_chunks_search_vector_update() RETURNS trigger AS $$
        DECLARE
            v_project_id integer;
        BEGIN
            SELECT project_id INTO v_project_id FROM search_sources WHERE id = NEW.source_id;
            NEW.search_vector := to_tsvector(specivo_fts_config(v_project_id), coalesce(NEW.content, ''));
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("DROP FUNCTION IF EXISTS specivo_fts_identifiers(text)")
