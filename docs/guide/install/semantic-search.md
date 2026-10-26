---
description: How Specivo's semantic search works and how to run it — what embeddings are for, the default multilingual-e5-small model, installing and backfilling it, offline setup, changing the model, and troubleshooting.
---

# Semantic search and embeddings

Specivo has two ways to find things. **Keyword search** matches words. **Semantic search** matches
meaning, and it needs an *embedding model* running on your server. This page explains what that
model does, how to switch it on, how to keep it working, and what to do when it does not.

Everything here is optional. Without an embedding model, Specivo works as usual and keyword search
keeps finding things by their words.

## What embeddings are for

An embedding is a list of numbers (384 of them, for the default model) that stands for the meaning
of a piece of text. Texts that mean similar things get similar numbers, even if they share no words.
Specivo stores one for each *chunk* of content and compares your search words against them.

Embeddings are used for **search, and only for search**. Nothing else in Specivo, such as
notifications, "related issues" or the board, uses them.

### Keyword, semantic and hybrid

| Mode | How it finds things | Good for | Needs a model |
|---|---|---|---|
| **Keyword** | PostgreSQL full-text search: your words, reduced to their stems in the project's search language | Exact names, error messages, ids | No |
| **Semantic** | Closeness of meaning to your words | "auth flow" finding an issue about the "login sequence" | Yes |
| **Hybrid** | Runs both and merges the two rankings, so a result both agree on rises to the top | Everything; the default for people | Yes, for the semantic half |

Issue keys such as `ACME-42` are matched directly and put first in every mode, with or without a
model.

Where each mode is offered:

- The **web search page** offers *Hybrid* (the default) and *Keyword*. People without an account get
  keyword search only, because embedding a query costs CPU.
- The **REST API** (`GET /api/v1/search/`) takes `mode=keyword` (the default), `semantic` or `hybrid`.
- **AI agents** over MCP use `hybrid` unless they ask for another mode. See
  [What agents can do](../ai-agents/capabilities.md).

### What you lose without a model

Nothing breaks and nothing errors. Hybrid search quietly becomes keyword search, and semantic search
returns only the issues whose key you typed. People still find work by its words, but not by its
meaning: a search for "auth flow" will not surface the issue titled "login sequence".

### What gets indexed

Content is cut into chunks, and each chunk gets an embedding:

| Content | Chunks |
|---|---|
| Issues | One chunk: the subject followed by the description |
| Wiki pages | One chunk per section (split at `#`, `##` and `###` headings, code blocks kept whole), each with the page title in front. Tiny sections are merged and sections over about 1,500 characters are split |
| Comments | One chunk per comment, if it has at least `SEARCH_MIN_COMMENT_LENGTH` characters (default 20). Comments written through an API key or by a service account are skipped by default |
| Attachments | One chunk of the filename and description, plus the extracted text and AI description when an attachment carries them |

The model reads at most 512 tokens of a chunk (roughly the first 2,000 characters); the rest of a
long chunk does not influence its embedding.

Related settings, all in `.env`:

| Variable | Default | Effect |
|---|---|---|
| `SEARCH_INDEX_COMMENTS` | `true` | `false` stops comments from being indexed |
| `SEARCH_MIN_COMMENT_LENGTH` | `20` | Shorter comments are not indexed |
| `SEARCH_EXCLUDE_BOT_COMMENTS` | `true` | Comments made through an API key or by a service account are not indexed |

### Vector index

Embeddings are stored in PostgreSQL with pgvector, and an **HNSW index** on them (created by
migration 0038) lets semantic and hybrid search find the nearest chunks without comparing the query
with every stored embedding. It covers 384-number vectors, the size `multilingual-e5-small` produces.

Search reads the nearest chunks from the index first and only then removes what the reader may not
see: other projects, private issues, the trash. When too few of those chunks are visible, it reads a
wider set from the index, and in the end compares every embedding in the projects the reader can
see. A visible match is never dropped because content the reader cannot see ranks ahead of it. The
cost is that someone who can see only a small part of a large instance gets slower semantic queries
than an administrator.

The index needs pgvector **0.8 or later**, for its iterative index scans; the bundled database image
has it. On an older pgvector, search compares every embedding and logs a warning once per process.

| Variable | Default | Effect |
|---|---|---|
| `SEARCH_HNSW_EF_SEARCH` | `100` | Candidates pgvector keeps per index scan (`hnsw.ef_search`). Higher finds the true nearest chunks more reliably, at some speed |
| `SEARCH_SEMANTIC_OVERFETCH` | `4` | Nearest chunks read per wanted result before invisible ones are removed. Raise it if many readers see only a few projects |

Index lookups are approximate: on a large instance a close match can rank a place or two
differently than an exact comparison would. Raising `SEARCH_HNSW_EF_SEARCH` narrows that.

## The default model

| | |
|---|---|
| Name | `multilingual-e5-small` (from the `intfloat/multilingual-e5-small` repository on Hugging Face, MIT license), pinned to revision `fd1525a9fd15` |
| Format | ONNX, run on the CPU with ONNX Runtime. No GPU, no external service, no API key |
| Size | About 487 MB to download: the model file is about 470 MB, the tokenizer about 17 MB |
| Vector size | 384 numbers per chunk |
| Languages | About 100 |
| Similarity | Cosine, on length-normalized vectors |

The model is a text-in, numbers-out network. Specivo adds the `passage: ` and `query: ` prefixes that
this model family expects, to content and to search text respectively. You do not need to do that.

### Where it lives

The model is **not** inside the Docker image, **not** in the release bundle and **not** downloaded on
first use. You put its files in a `models` folder inside your data directory:

```
<SPECIVO_DATA_DIR>/models/multilingual-e5-small.onnx
<SPECIVO_DATA_DIR>/models/tokenizer.json
```

`SPECIVO_DATA_DIR` defaults to `./specivo-data`, which Docker Compose mounts at `/app/data` in the
`api`, `celery-worker` and `celery-embeddings` containers, so the files are found at
`/app/data/models/`. They are also covered by your normal [backup](backup-restore.md) of the data
directory.

Those two files are all Specivo reads, and all the download command fetches.

**There is no on/off setting.** The files being present is the switch. Specivo looks for them each
time it needs an embedding, so you do not have to restart after adding them. A fresh install already
has the model registered in the database (the entrypoint does this), so adding the files is all it
takes.

### CPU and memory

- The model is loaded into memory the first time it is used by a process, and it stays there. Budget
  at least the size of the model file for each process that embeds: the `api` container, which
  embeds search queries, and the `celery-embeddings` container, which embeds content. Embedding jobs
  go to a queue of their own (`embeddings`) that only `celery-embeddings` reads, and it runs **one
  process** by default, so content embedding keeps one copy of the model in memory however many
  cores you have. The general `celery-worker` never loads the model. Measure on your host; a small
  team fits the 2 CPU / 2 to 4 GB guidance in the [overview](index.md).
- To embed faster on a big host, set `EMBEDDING_WORKER_CONCURRENCY` in `.env` (default `1`); each
  extra process costs another copy of the model. To run embedding in the general worker instead of
  a separate container, set `EMBEDDING_QUEUE=celery` and stop `celery-embeddings`. Restart the stack
  after changing either.
- Inference uses **2 CPU threads** by default. Set `EMBEDDING_ONNX_THREADS` in `.env` to change that:
  a higher number makes indexing faster and uses more CPU, and `0` lets the model use every core.
  Restart the stack afterwards.
- Embedding a search query is a single short text, which is cheap. Indexing a large instance is the
  heavy part (see [Index existing content](#index-existing-content)).

## Turn it on

1. **Download the model** into the data directory, from inside the running `api` container:

    ```bash
    docker compose exec api python -m specivo.cli.download_model
    ```

    `make download-model` runs the same command, from a checkout or from the release bundle. It
    needs the `api` container to reach `huggingface.co` (set `HTTPS_PROXY` for the container if you
    use a proxy) and writes to `/app/data/models`, which is `<SPECIVO_DATA_DIR>/models` on the host.
    Each file comes from a pinned revision and is checked against its SHA-256 before it replaces
    anything; a file that fails the check is discarded and the command exits with an error. Files
    already present with the right checksum are skipped, so it is safe to run again. `--force`
    downloads them anyway. When the host is offline, the command stops with the list of files, their
    URLs and checksums.

    `scripts/download-model.sh` (in the repository and the release bundle) does the same on a host
    without Docker, for example to prepare the files for an air-gapped server:
    `bash scripts/download-model.sh /path/to/models`. It needs `curl` and `sha256sum` or `shasum`.

2. **Index the content you already have** (below). New content is picked up as described under
   [How content is indexed](#how-content-is-indexed).

3. **Check that it works** (below).

For a host without internet, download on a connected machine and copy the `models` folder across, as
described in [Air-gapped install](air-gapped.md#semantic-search-offline).

## How content is indexed

Indexing happens in two steps, and only the second needs the model.

1. **Chunks, at once.** When an issue, wiki page, comment or attachment is created, edited or
   deleted, its chunks are written, replaced or removed in the same database transaction as the
   change. Keyword search reads them straight away. This needs no model and no worker, and it is
   cheap enough not to slow saving down.
2. **Embeddings, a few seconds later.** After the change is saved, Specivo queues a job for the
   `celery-embeddings` worker, which embeds the new chunks. Semantic and hybrid search find the content once
   that job has run, usually within seconds. Quick edits in a row are usually embedded once, from
   the latest text.

This covers every way content changes: the web UI, the REST API, agents over MCP, recurring issues,
moving an issue to another project (its chunks, comments and attachments move with it) and commit
messages that reference an issue. An import indexes everything it brought in at the end of the run.

| Event | Keyword search | Semantic search |
|---|---|---|
| An issue, wiki page, comment or attachment is created or edited | Immediately | Once the worker has embedded it |
| One is deleted, or a wiki page is moved to the trash | Gone immediately | Gone immediately |
| A wiki page is restored from the trash | Immediately | Once the worker has embedded it |
| A comment is made private | Hidden from people who cannot see private notes immediately | Same |

Things to know:

- **The `celery-embeddings` worker must be running for semantic indexing.** The standard Compose
  file starts it. Without it, keyword search still finds everything, but new and edited content
  waits for the worker (or the backfill below) before semantic search finds it. The queued jobs are
  kept in Redis and run when the worker comes back.
- **Without the model files nothing is queued.** Chunks are still written, so keyword search is
  complete. When you add the files later, run the backfill once to embed what was written in the
  meantime. The embedding worker logs at startup whether semantic indexing is on.
- If Redis cannot be reached when a change is saved, the change is still saved and keyword-searchable;
  the `api` logs a warning, and the backfill embeds the content later.
- Indexing problems are logged at warning level, with the kind of content and its id. They never make
  the save itself fail.
- The comment settings above are applied as comments are written and edited. Changing them later
  affects comments written or edited afterwards; run the backfill to apply them to older comments
  too.

## Index existing content

The backfill goes through every issue, wiki page, comment and attachment, brings its chunks in line
with the content and the current settings, and embeds the chunks that have no embedding yet. It
indexes every attachment, with or without a description, whether it hangs off an issue, a comment
or a wiki page. It removes the chunks of comments the comment settings now exclude, and of items
that no longer exist. It is safe to run again whenever you like: chunks whose text has not changed
keep their embeddings. Run it after you add the model files, and after you change a comment
setting. Day to day there is no need to run it, because new and edited content is indexed as it is
saved.

```bash
docker compose exec api python -m specivo.cli.backfill_embeddings
```

`make backfill-embeddings` runs the same command (`make backfill-embeddings ARGS=--reembed` passes
options). It runs in the foreground, prints progress, and ends with what it did:

```
Chunked 1840 sources into 2312 chunks; removed 3 stale sources.
Embedded 2312 chunks with multilingual-e5-small; 2312 of 2312 chunks now have an embedding.
```

Work is saved in small batches, so stopping it with Ctrl+C loses very little, and you can start it
again. The time it takes grows with the number of chunks and your CPU. Run a large first backfill
when the server is quiet, and raise `EMBEDDING_ONNX_THREADS` for the duration if you have cores to
spare.

| Option | Effect |
|---|---|
| `--reembed` | Recompute the embedding of every chunk, not only of chunks that have none. Use it after replacing the model files. Old embeddings are replaced one by one, so semantic search keeps working meanwhile |
| `--require-model` | Stop with exit status 2, without doing anything, when no model can run. Useful in scripts that expect semantic search to be on |

The exit status is 0 when the run succeeded, with or without a model, and 1 when some items could
not be indexed (the log names each one).

!!! note "Without the model it writes keyword chunks only"
    If the model files are missing, the backfill still writes every chunk, which is all keyword
    search needs, and embeds nothing. It says so at the end with a warning: *Only keyword-search
    chunks were written; 0 chunks were embedded and semantic search stays off.* Add the files and
    run it again for semantic search.

An administrator can also start a backfill for one registered model in the background with
`POST /api/v1/admin/embedding-models/{id}/backfill/`. It runs on the `celery-embeddings` worker and
embeds the chunks that have none for that model; add `?reembed=true` to recompute every embedding,
for example after replacing the model files. It does not rebuild chunk text (that is kept current
as content is saved, and the command above rebuilds it). It answers `409` when the model cannot run
here, for example because its files are missing. It needs an administrator account; there is no
button for it in the web UI.

## Check that it works

1. **Are the files there?**

    ```bash
    docker compose exec api ls -l /app/data/models
    ```

    You should see `multilingual-e5-small.onnx` and `tokenizer.json`.

2. **Does the log agree?** The embedding worker says at startup whether semantic indexing is on,
   and each process logs the line below when it first loads the model. When the model is missing,
   the `api` logs a warning each time it needs to embed a search query.

    ```bash
    docker compose logs celery-embeddings api | grep -iE "semantic indexing|embedding model|ONNX model"
    ```

    ```
    Semantic indexing is on: embedding with multilingual-e5-small
    Loaded ONNX model multilingual-e5-small from /app/data/models/multilingual-e5-small.onnx (threads=2)
    Local embedding model 'multilingual-e5-small' is not available (missing ONNX/tokenizer files)
    ```

3. **Is anything indexed?** Compare chunks with embeddings. Once the worker has caught up they should
   be close to equal.

    ```bash
    docker compose exec db psql -U specivo specivo -c \
      "SELECT (SELECT count(*) FROM search_chunks) AS chunks,
              (SELECT count(*) FROM chunk_embeddings) AS embeddings;"
    ```

    (Use your `POSTGRES_USER` and `POSTGRES_DB` if you changed them.) Many chunks and zero
    embeddings means the chunks were built while the model files were missing, or the
    `celery-embeddings` worker is not running. Check the worker, add the files, and run the backfill
    again.

4. **Does a meaning-based search work?** Search for a paraphrase of something you know is in an
   issue, using an [API key](../ai-agents/api-keys.md):

    ```bash
    curl -H "Authorization: Bearer spv_your_api_key_here" \
      "https://your-specivo-host/api/v1/search/?q=sign-in+flow&mode=semantic"
    ```

    Results that do not contain your words mean semantic search is working.

## Changing the model

**Only the bundled local model, `multilingual-e5-small`, is supported.** Hosted embedding providers
and bring-your-own-key (BYOK) setups are not available, and switching to a different model is not
supported either. There is no setting for it and no screen for it.

- Models are rows in the `embedding_models` table, and search uses the one marked as default. The
  seed command registers the local model (it runs each time the `api` container starts). The
  administrator REST API under `/api/v1/admin/embedding-models/` can list models, delete one and
  start a [backfill](#index-existing-content), but it cannot register a model.
- The database allows at most one default model. Upgrading to 0.9.0 (migration 0039) clears the
  extra defaults if an older release left more than one, keeping the local model.
- If the default row names any other provider, for example one registered by an older release,
  Specivo treats it like a model whose files are missing: it logs one warning, embeds nothing, and
  hybrid search returns keyword results. Delete that row and restart the `api` container so the seed
  registers the local model again, then run the backfill.
- Vectors from different models cannot be compared, and the [vector index](#vector-index) only
  covers the 384 numbers of `multilingual-e5-small`. Moving to another model will need a full
  rebuild of the index, and this page will describe it if that becomes supported.

## Troubleshooting

**Semantic or hybrid search finds nothing beyond keyword results.**
Work down the list in [Check that it works](#check-that-it-works): the files in
`/app/data/models`, the log line, and the chunks and embeddings counts. The usual causes are no
model files, files in the wrong folder (it must be the `models` folder *inside* the folder mounted
as `/app/data`, and `SPECIVO_DATA_DIR` in `.env` must point at the same place for all containers), a
stopped `celery-embeddings` worker, or a backfill that has not run since the files were added.

**A result I know exists is missing, or looks out of date.**
In keyword mode, check that you can open the item: search only shows what you are allowed to see,
and comments shorter than `SEARCH_MIN_COMMENT_LENGTH` or written by a bot are not indexed. In
semantic mode, the `celery-embeddings` worker may not have embedded the latest text yet; check that
it is running and look for warnings in its log. Content from before 0.9.0 that was never backfilled may lack
embeddings; run the backfill. See [How content is indexed](#how-content-is-indexed).

**The model download fails.**
*Cannot reach huggingface.co* means the `api` container has no route to Hugging Face. Behind a
proxy, set `HTTPS_PROXY` for the container; behind a firewall, allow that host; otherwise download on
another machine with `scripts/download-model.sh` and copy the files over (see
[Air-gapped install](air-gapped.md#semantic-search-offline)). The error lists both files with their
URLs and checksums. *Checksum mismatch* means the file that arrived is not the pinned one, for
example because a proxy altered it; it was discarded and nothing was replaced. A failed download
leaves no partial file, and running the command again skips the files that are already complete.

**Indexing is slow.**
Embedding is one chunk at a time on the CPU. Raise `EMBEDDING_ONNX_THREADS` while you backfill, and
give the `api` and `celery-embeddings` containers more cores, or raise
`EMBEDDING_WORKER_CONCURRENCY` if you have memory for another copy of the model. Saving is not slowed down: content is
embedded by the worker after it is saved, so a burst of changes only means semantic search catches up
a little later.

**Searching is slow on a large instance.**
Semantic search reads the nearest chunks from the [vector index](#vector-index), so its cost grows
slowly with the number of chunks. Check that the index exists and is valid:

    docker compose exec db psql -U specivo -d specivo -c \
      "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('ix_chunk_embeddings_hnsw_384')"

`t` is what you want. No row means it was not built, because the pgvector extension is older than
0.5 (the upgrade log says so). `f` means it is broken; rebuild it without blocking search with
`REINDEX INDEX CONCURRENTLY ix_chunk_embeddings_hnsw_384` in the same `psql`. Check the pgvector
version too (`SELECT extversion FROM pg_extension WHERE extname = 'vector'`): before 0.8 the index
is not used. With the index in place, a reader who can see only a few projects of a large instance is the
slow case, because search widens its candidate set until it has enough results they may see; a
higher `SEARCH_SEMANTIC_OVERFETCH` gets there in fewer steps. Hybrid search also runs a keyword
search, which is often the larger part of its time.

**The `api` or `celery-embeddings` container uses a lot of memory.**
The model is loaded into each process the first time it embeds something and stays loaded. This is
expected: the model file alone is about 470 MB. `celery-embeddings` runs one process by default; if
you raised `EMBEDDING_WORKER_CONCURRENCY`, lower it. If memory is still tight, give the containers
more, or leave the model files out and use keyword search. The general `celery-worker` does not load
the model; if it does, check that `EMBEDDING_QUEUE` is not set to `celery`.
