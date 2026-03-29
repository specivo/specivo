-- Auto-create extensions required by Specivo.
-- Mounted into /docker-entrypoint-initdb.d/ so it runs on first DB init.
CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS vector;
