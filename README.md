# Specivo

Your team's knowledge, finally findable.

Self-hosted project tracking, wiki, and search that understands what you wrote. Open source, no per-seat pricing.

## The problem

Teams lose knowledge constantly. Decisions get made in Slack and vanish. Architecture docs end up in five different tools. Somebody leaves and takes half the context with them. AI agents make it worse by generating content faster than anyone can organize.

Specivo puts issues, wiki, and search in one place. The search part is the point: you wrote "login flow" in January and "auth sequence" in March, and keyword search can't connect the two. Specivo's hybrid search (full-text + semantic) can.

## Quick start

```bash
git clone https://github.com/specivo/specivo.git
cd specivo
docker compose up
```

Open http://localhost:9933.

## What's in it

**Issues** with hierarchy (nested set, up to 16 levels), a workflow engine, time tracking, versions, relations, watchers, bulk operations.

**Wiki** with Markdown, full version history, page hierarchy, one-click revert. Every edit tracked.

**Search** across issues, wiki, comments, and attachments. Full-text via PostgreSQL tsvector, semantic via pgvector embeddings, fused with RRF. Finds things by meaning, not just exact words. Attachments are searchable by their description — find that architecture diagram by what it shows, not just its filename.

**MCP server** with 11 tools. Works with Claude Code, Codex, or anything that speaks MCP. One config line and the agent stops guessing.

**AI is optional.** Works fine without it. Turn on semantic search when you want it, using the bundled model (multilingual-e5-small, 100 languages, runs on CPU) or your own API key.

**i18n** via .po files. Ships with English and Thai.

**Themes.** Bootstrap-based, CSS custom properties. Override or build your own.

## Tech

- Python 3.12, FastAPI, SQLAlchemy 2.0 async, Pydantic V2
- PostgreSQL 18 with ltree and pgvector, Redis
- Celery for background jobs
- Jinja2, Alpine.js, htmx on the frontend

## Development

Needs [uv](https://docs.astral.sh/uv/).

```bash
make install          # dependencies
make dev-up           # dev server with hot-reload
make test-db-up       # test database
make test             # run tests
make lint             # ruff + mypy
make download-model   # embedding model (~393 MB)
```

## License

[AGPL-3.0-or-later](LICENSE)
