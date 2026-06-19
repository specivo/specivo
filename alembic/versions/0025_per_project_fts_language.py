"""Per-project full-text-search analyzer language.

Create Date: 2026-06-19
Revision ID: 0025
Revises: 0024

Makes the FTS analyzer language configurable per project (with an instance-wide
default every project inherits), applied at INDEX time. Previously the three
indexing sites (issues / wiki_contents triggers and the search_chunks generated
column) hardcoded ``to_tsvector('english', …)``, so non-English content was never
stemmed correctly.

- Adds ``projects.fts_language`` (NULL = inherit instance default).
- Adds ``specivo_fts_config(project_id)`` → resolves project override → instance
  default (``settings.search_fts_language``) → validated regconfig → ``english``.
- Rewrites both trigger functions to resolve the row's project language.
- Converts ``search_chunks.search_vector`` from a generated column to a
  trigger-maintained column (a generated column cannot read a runtime setting).
- Seeds the instance-default settings row from ``SEARCH_FTS_LANGUAGE`` if absent
  and reindexes all existing rows once.
"""

from alembic import op

revision = "0025"
down_revision = "0024"


def upgrade() -> None:
    # 1. Per-project override column (NULL = inherit instance default).
    op.execute("ALTER TABLE projects ADD COLUMN fts_language varchar(32)")

    # 2. Seed the instance-default setting from the configured env value if the
    #    row is absent. Stored as a plain string (SettingsService stores Text).
    try:
        from specivo.core.config import get_settings

        default_lang = get_settings().search_fts_language
    except Exception:
        default_lang = "english"
    op.execute(
        f"""
        INSERT INTO settings (key, value, created_at, updated_at)
        VALUES ('search_fts_language', '{default_lang}', now(), now())
        ON CONFLICT (key) DO NOTHING
        """
    )

    # 3. Resolver: project override -> instance default -> validated -> english.
    op.execute("""
        CREATE OR REPLACE FUNCTION specivo_fts_config(p_project_id integer)
        RETURNS regconfig AS $$
        DECLARE
            v_lang text;
        BEGIN
            IF p_project_id IS NOT NULL THEN
                SELECT fts_language INTO v_lang FROM projects WHERE id = p_project_id;
            END IF;
            IF v_lang IS NULL OR v_lang = '' THEN
                SELECT value INTO v_lang FROM settings WHERE key = 'search_fts_language';
            END IF;
            IF v_lang IS NULL OR NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = v_lang) THEN
                v_lang := 'english';
            END IF;
            RETURN v_lang::regconfig;
        END;
        $$ LANGUAGE plpgsql STABLE
    """)

    # 4. Issues trigger — resolve language from the row's project.
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

    # 5. Wiki contents trigger — resolve project via wiki_pages -> wikis.
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

    # 6. Convert search_chunks.search_vector: generated column -> trigger column.
    #    Dropping the column also drops the dependent GIN index ix_search_chunks_fts.
    op.execute("ALTER TABLE search_chunks DROP COLUMN search_vector")
    op.execute("ALTER TABLE search_chunks ADD COLUMN search_vector tsvector")
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
    op.execute("""
        CREATE TRIGGER trg_search_chunks_search_vector
            BEFORE INSERT OR UPDATE OF content ON search_chunks
            FOR EACH ROW EXECUTE FUNCTION search_chunks_search_vector_update()
    """)

    # 7. Reindex existing rows once (fire the triggers), then (re)build the
    #    search_chunks GIN index after the column is populated.
    op.execute("UPDATE issues SET subject = subject")
    op.execute("UPDATE wiki_contents SET text = text")
    op.execute("UPDATE search_chunks SET content = content")
    op.execute("CREATE INDEX ix_search_chunks_fts ON search_chunks USING gin(search_vector)")


def downgrade() -> None:
    # Restore the search_chunks generated column.
    op.execute("DROP TRIGGER IF EXISTS trg_search_chunks_search_vector ON search_chunks")
    op.execute("DROP FUNCTION IF EXISTS search_chunks_search_vector_update()")
    op.execute("ALTER TABLE search_chunks DROP COLUMN search_vector")
    op.execute(
        "ALTER TABLE search_chunks ADD COLUMN search_vector tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', content)) STORED"
    )
    op.execute("CREATE INDEX ix_search_chunks_fts ON search_chunks USING gin(search_vector)")

    # Restore the hardcoded-english trigger functions (matching migration 0013).
    op.execute("""
        CREATE OR REPLACE FUNCTION wiki_contents_search_vector_update() RETURNS trigger AS $$
        DECLARE
            page_title text;
        BEGIN
            SELECT title INTO page_title FROM wiki_pages WHERE id = NEW.page_id;
            NEW.search_vector :=
                setweight(to_tsvector('english', replace(coalesce(page_title, ''), '_', ' ')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.text, '')), 'B');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION issues_search_vector_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector('english', replace(coalesce(NEW.subject, ''), '_', ' ')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.description, '')), 'B');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("UPDATE issues SET subject = subject")
    op.execute("UPDATE wiki_contents SET text = text")

    op.execute("DROP FUNCTION IF EXISTS specivo_fts_config(integer)")
    op.execute("ALTER TABLE projects DROP COLUMN fts_language")
