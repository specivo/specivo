"""Index text with unclosed ``<!--`` in linear time.

Create Date: 2026-10-27
Revision ID: 0040
Revises: 0039

PostgreSQL's default text-search parser, shared by every built-in regconfig
(``simple`` included), reads ``<!--`` as the start of an HTML comment and
scans ahead for ``-->``. When there is none, it gives up, moves on one
character and tries again at the next ``<!--``: each unclosed opener rereads
the rest of the text. 16,000 of them take about 5 s, and text at the
262,144-character Markdown limit tens of seconds, on every save (in the
search-vector triggers) and on every search that shows a snippet of it
(``ts_headline``). Closed comments, other tags, URLs, e-mail addresses and
long words are all linear.

``specivo_fts_text(text)`` removes the cost without changing what is
indexed: an opener that has no ``-->`` after it yields exactly the tokens it
would yield with its ``<`` replaced by a space, so after the last ``-->`` in
the text every ``<!--`` becomes `` !--``. Text before the last ``-->`` is
left alone; there, every opener has a closer and the parser reads each
comment once. The function is linear (``strpos``, ``reverse``, ``replace``).

The three trigger functions from 0034 are redefined to pass their text
through it; the search service does the same for ``ts_headline`` and for the
query given to ``plainto_tsquery``. Since vectors for any text come out
identical, nothing is reindexed.
"""

from alembic import op

revision = "0040"
down_revision = "0039"


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION specivo_fts_text(p_text text) RETURNS text AS $$
            SELECT CASE
                WHEN strpos(t, '<!--') = 0 THEN t
                ELSE left(t, keep) || replace(substr(t, keep + 1), '<!--', ' !--')
            END
            FROM (
                SELECT t,
                       CASE WHEN strpos(reverse(t), '>--') = 0 THEN 0
                            ELSE length(t) - strpos(reverse(t), '>--') + 1
                       END AS keep
                FROM (SELECT coalesce(p_text, '') AS t) AS source
            ) AS split
        $$ LANGUAGE sql IMMUTABLE PARALLEL SAFE
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION issues_search_vector_update() RETURNS trigger AS $$
        DECLARE
            v_cfg regconfig := specivo_fts_config(NEW.project_id);
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector(v_cfg, specivo_fts_text(replace(coalesce(NEW.subject, ''), '_', ' '))), 'A') ||
                setweight(specivo_fts_identifiers(NEW.subject), 'A') ||
                setweight(to_tsvector(v_cfg, specivo_fts_text(NEW.description)), 'B') ||
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
                setweight(to_tsvector(v_cfg, specivo_fts_text(replace(coalesce(page_title, ''), '_', ' '))), 'A') ||
                setweight(specivo_fts_identifiers(page_title), 'A') ||
                setweight(to_tsvector(v_cfg, specivo_fts_text(NEW.text)), 'B') ||
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
                to_tsvector(specivo_fts_config(v_project_id), specivo_fts_text(NEW.content)) ||
                specivo_fts_identifiers(NEW.content);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)


def downgrade() -> None:
    # Restore the 0034 trigger functions, then drop the helper they no longer use.
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
    op.execute("DROP FUNCTION IF EXISTS specivo_fts_text(text)")
