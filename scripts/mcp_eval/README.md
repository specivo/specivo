# MCP tool listing evaluation

Compares how well a model works with Specivo's MCP tools when `tools/list` carries the **full**
descriptions (the docstrings and full parameter texts, as advertised before short descriptions)
and when it carries the **lean** ones (summaries, short parameter texts, full help on demand). Both
listings come from the same code: `specivo.mcp.tool_help.tool_listing_var` switches them.

It is not part of the test suite and never runs in CI: `pytest` collects only `tests/`.

## What it does

1. Creates fixture data in the database `DATABASE_URL` points at: the default lookups, an
   administrator with an API key, and two projects with a random key (`EV....`, `EQ....`). Issues,
   wiki pages, a version, a sprint, a tag, a metadata schema, a relation and a recurring pattern
   are created through the MCP tools (`fixture.py`). Every run (and each listing mode) gets fresh
   projects, so runs never collide; the data is committed, so **use a throwaway database**.
2. Serves the in-process MCP server (`specivo.mcp.server.mcp`) with the chosen listing and toolsets
   (default `all`).
3. Runs the 52 scenarios of `scenarios.py`, one after the other: a prompt and the known correct
   tool sequence (create an issue with metadata and tags, change a status, log time, read and edit
   a wiki section, add and remove a relation, search, link a wiki page, list a version's issues,
   comment, start and stop the timer, recurring patterns, AI usage, and more).

For every task it records:

| Field | Meaning |
|---|---|
| `first_call_right` | The first call that is not a help lookup uses a tool of the expected first step. |
| `schema_rejected` | Calls refused before running: arguments failing validation, unknown tool, broken JSON. |
| `calls` | Tool calls until the model stopped calling tools (at most 12 model turns). |
| `success` | Every required step was matched, in order, by a call that succeeded. |
| `help_lookups` | Calls of `specivo_setup_guide(tool=...)`. |
| `input_tokens`, `output_tokens` | Summed from the endpoint's `usage` over all turns of the task. |

and prints a table per listing mode (tools/list size, first-call accuracy, success rate, schema
rejections, calls per task, help lookups, tokens and tokens per finished task).

## Usage

Run from the repository root, with a database that may be written to (here a separate database
on the test stack, so the test database stays clean):

```bash
# once: a throwaway database next to the test database
docker compose -f docker-compose.test.yml exec test-db \
  psql -U specivo -d specivo_test -c "CREATE DATABASE specivo_mcp_eval"
export DATABASE_URL=postgresql+asyncpg://specivo:specivo@localhost:5433/specivo_mcp_eval
export REDIS_URL=redis://localhost:6380/1
export SECRET_KEY=any-string-of-at-least-32-characters-long
uv run alembic upgrade head

# validate the scenarios and the scoring without a model
uv run python -m scripts.mcp_eval --dry-run
```

`--dry-run` runs each scenario's reference calls against the server and scores them; it fails
(exit status 1) when a reference call errors or does not satisfy its own scenario.

With a model, through any OpenAI-compatible chat completions endpoint with tool calling:

```bash
# OpenRouter
export MCP_EVAL_BASE_URL=https://openrouter.ai/api/v1 MCP_EVAL_MODEL=<model id> MCP_EVAL_API_KEY=...
# Anthropic's OpenAI-compatible endpoint
export MCP_EVAL_BASE_URL=https://api.anthropic.com/v1 MCP_EVAL_MODEL=<model id> MCP_EVAL_API_KEY=...
# A local Ollama (no key)
export MCP_EVAL_BASE_URL=http://localhost:11434/v1 MCP_EVAL_MODEL=qwen3:8b

uv run python -m scripts.mcp_eval --mode both --out results.json
```

A full run is 52 tasks per mode (104 for `both`), each a few model turns with the whole tool list
in the prompt: check the price before pointing it at a paid model.

Options:

- `--mode both|lean|full` - which listing(s) to serve (default `both`).
- `--toolsets core,planning,...` - the toolsets the connection loads (default `all`; scenarios
  for tools outside them then fail, which shows what the toolset error message costs).
- `--only create-bug,log-time` - run some scenarios only.
- `--out results.json` - every task result as JSON.

## Adding a scenario

Add a `Scenario` to `SCENARIOS`: an id, a prompt (`{P}` is the main project, `{Q}` the second,
`{login}` the agent) and its steps. Each `Step` names the acceptable tools (the first is used by
the dry run), the reference arguments `ref` and the arguments a model must match in `check`
(substring for text, value for numbers; `"@name"` is a fixture ID such as `@tracker_bug`).
Mark a lookup a model may skip with `optional=True`. Run `--dry-run` afterwards.
