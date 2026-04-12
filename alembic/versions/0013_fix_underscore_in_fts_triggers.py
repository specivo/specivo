"""Fix underscore tokenization in FTS triggers.

Create Date: 2026-04-12
Revision ID: 0013
Revises: 0012

PostgreSQL to_tsvector treats underscores as part of the token:
    to_tsvector('english', 'SEO_Strategy') → 'seo_strategi':1
This produces a single compound token that matches neither 'seo' nor
'strategi', making exact title matches invisible to FTS.

Fix: replace('_', ' ') in both wiki and issue tsvector triggers.
"""

from alembic import op

revision = "0013"
down_revision = "0012"


def upgrade() -> None:
    # Fix wiki trigger: replace underscores in title before tokenizing
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

    # Fix issue trigger: replace underscores in subject before tokenizing
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

    # Reindex all wiki pages by touching the text column (triggers tsvector rebuild)
    op.execute("UPDATE wiki_contents SET text = text")

    # Reindex all issues by touching the subject column
    op.execute("UPDATE issues SET subject = subject")


def downgrade() -> None:
    # Restore original triggers without replace()
    op.execute("""
        CREATE OR REPLACE FUNCTION wiki_contents_search_vector_update() RETURNS trigger AS $$
        DECLARE
            page_title text;
        BEGIN
            SELECT title INTO page_title FROM wiki_pages WHERE id = NEW.page_id;
            NEW.search_vector :=
                setweight(to_tsvector('english', coalesce(page_title, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.text, '')), 'B');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION issues_search_vector_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector('english', coalesce(NEW.subject, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.description, '')), 'B');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    op.execute("UPDATE wiki_contents SET text = text")
    op.execute("UPDATE issues SET subject = subject")
