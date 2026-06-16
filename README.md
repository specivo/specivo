<p align="center">
  <a href="https://specivo.io"><img src="https://specivo.io/static/img/og-cover.jpg" alt="Specivo — your team's knowledge, finally findable" width="820"></a>
</p>

# Specivo

Your team's knowledge, finally findable.

Self-hosted project tracker with wiki and semantic search. Open source. No per-seat pricing. Built for teams that also use AI agents.

## The problem

Teams lose knowledge constantly. Decisions get made in chat and vanish. Architecture docs scatter across five tools. AI coding agents make it worse — they ship code in a black box. What was done, why, and how it connects to other work becomes impossible to track after the session ends.

Specivo fixes this by letting agents work inside the tracker, not outside it. Users plan sprints with help from agents, assign issues to people or agents, and review what was done in a familiar issue tracker UI. Agents create issues, update wiki pages, log time, and manage versions through MCP tools — one config line, no glue code. Every action is tracked, searchable, and connected. Assign tasks to different models, track outcomes, and see which agents deliver quality and which produce slop that humans had to fix.

You wrote "login flow" in January and "auth sequence" in March — keyword search cannot connect the two. Specivo's hybrid search (full-text + semantic) finds things by meaning, not just exact words.

## Quick start

```bash
git clone https://github.com/specivo/specivo.git
cd specivo
docker compose up -d
```

Create your first admin user:

```bash
docker compose exec api python -m specivo.cli.admin create \
  --login admin --email admin@localhost --password changeme
```

Open http://localhost:9933 and sign in.

For production, copy `.env.example` to `.env` and set a real `SECRET_KEY`.

Step-by-step self-hosting guide: [Self-host Specivo with Docker](https://specivo.io/self-host-specivo-docker/).

## What's in it

**Issues** — hierarchy up to 16 levels (nested set), workflow engine, time tracking, versions, relations (9 types), watchers, bulk operations, custom metadata schemas.

**Wiki** — Markdown, full version history, page hierarchy, one-click revert. Every edit tracked. Cross-linked with issues.

**Hybrid search** — full-text via PostgreSQL tsvector, semantic via pgvector embeddings, fused with Reciprocal Rank Fusion. Searches issues, wiki, comments, and attachments. Attachment descriptions are searchable: find that architecture diagram by what it shows, not its filename.

**MCP server** — Model Context Protocol tools. Works with Claude Code, Codex, or anything that speaks MCP. Issues, wiki pages, comments, and attachment descriptions become searchable context that agents can pull and update — automatically or on user confirmation. One config line to connect. Ready-made agent instructions for Claude Code, Codex, and Cursor live in [**specivo-agent-skills**](https://github.com/specivo/specivo-agent-skills).

**Sprint management** — backlog, sprint planning, start/complete cycles. Issues move between sprints with full history preserved.

**Custom metadata schemas** — define structured fields per project or issue type. Preset schemas for common workflows. More flexible than static custom fields.

**AI is optional.** Works fine without it. Enable semantic search when you want it, using the bundled model (multilingual-e5-small, runs on CPU, 100 languages) or your own API key (BYOK).

**i18n** — ships with English, Russian, Chinese, French, Spanish and Thai. Per-user language and workspace default. Extensible via `.po` files.

## Tech

- Python 3.12, FastAPI, SQLAlchemy 2.0 async, Pydantic V2
- PostgreSQL 18 with ltree and pgvector, Redis
- Celery for background jobs
- Jinja2, Alpine.js, htmx on the frontend

No Elasticsearch. No external search service. Search runs entirely inside PostgreSQL.

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

## Documentation

[specivo.io/docs/specivo](https://specivo.io/docs/specivo/)

**For AI agents:** [specivo-agent-skills](https://github.com/specivo/specivo-agent-skills) — drop-in conventions that teach Claude Code, Codex, Cursor, and any MCP client to use Specivo's tools well.

## License

[AGPL-3.0-or-later](LICENSE)
