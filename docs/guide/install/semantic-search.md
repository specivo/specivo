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

## The default model

| | |
|---|---|
| Name | `multilingual-e5-small` (from the `intfloat/multilingual-e5-small` repository on Hugging Face, MIT license) |
| Format | ONNX, run on the CPU with ONNX Runtime. No GPU, no external service, no API key |
| Size | About 393 MB to download; the model file itself is about 370 MB |
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

`SPECIVO_DATA_DIR` defaults to `./specivo-data`, which Docker Compose mounts at `/app/data` in both
the `api` and the `celery-worker` containers, so the files are found at `/app/data/models/`. They are
also covered by your normal [backup](backup-restore.md) of the data directory.

Those two files are the only ones Specivo checks for. The download script fetches three more
(`tokenizer_config.json`, `special_tokens_map.json`, `sentencepiece.bpe.model`) that the loader
does not read.

**There is no on/off setting.** The files being present is the switch. Specivo looks for them each
time it needs an embedding, so you do not have to restart after adding them. A fresh install already
has the model registered in the database (the entrypoint does this), so adding the files is all it
takes.

### CPU and memory

- The model is loaded into memory the first time it is used by a process, and it stays there. Budget
  at least the size of the model file for each process that embeds: the `api` container, and the
  `celery-worker` while it runs a model backfill. Measure on your host; a small team fits the
  2 CPU / 2 to 4 GB guidance in the [overview](index.md).
- Inference uses **2 CPU threads** by default. Set `EMBEDDING_ONNX_THREADS` in `.env` to change that:
  a higher number makes indexing faster and uses more CPU, and `0` lets the model use every core.
  Restart the stack afterwards.
- Embedding a search query is a single short text, which is cheap. Indexing a large instance is the
  heavy part (see [Index existing content](#index-existing-content)).

## Turn it on

1. **Download the model** into the data directory. From a checkout of the repository:

    ```bash
    make download-model
    ```

    This needs `curl` and a connection to `huggingface.co`, and writes to
    `${SPECIVO_DATA_DIR:-specivo-data}/models`. If your data directory is somewhere else, give the
    path: `bash scripts/download-model.sh /path/to/data/models`. The script skips files that are
    already there.

    Without a checkout (for example from the release bundle, which does not contain the script),
    fetch the files by hand:

    ```bash
    mkdir -p specivo-data/models && cd specivo-data/models
    BASE=https://huggingface.co/intfloat/multilingual-e5-small/resolve/main
    curl -L "$BASE/onnx/model.onnx"        -o multilingual-e5-small.onnx
    curl -L "$BASE/tokenizer.json"         -o tokenizer.json
    curl -L "$BASE/tokenizer_config.json"  -o tokenizer_config.json
    curl -L "$BASE/special_tokens_map.json" -o special_tokens_map.json
    curl -L "$BASE/sentencepiece.bpe.model" -o sentencepiece.bpe.model
    ```

2. **Index the content you already have** (below). New content is picked up as described under
   [How content is indexed](#how-content-is-indexed).

3. **Check that it works** (below).

For a host without internet, download on a connected machine and copy the `models` folder across, as
described in [Air-gapped install](air-gapped.md#semantic-search-offline).

## How content is indexed

New content is embedded **inside the request that creates it**, by the `api` process. It does not go
through the Celery worker, so the worker does not have to be running for this. Failures are not
reported to the person who saved: the content is saved and simply is not indexed.

Here is exactly what is indexed automatically:

| Event | Indexed automatically |
|---|---|
| An issue is created | Yes |
| An issue is moved to another project | Yes |
| An issue's subject or description is edited | **No** |
| A wiki page is created, or restored from the trash | Yes |
| A wiki page is edited | **No** |
| A comment is added | **No** |
| An attachment is uploaded, or its description edited | Yes |
| A wiki page is deleted | Its index entries are removed |
| An import runs | Imported issues and wiki pages are indexed in the import's last phase. Imported comments are **not** |

!!! warning "Edits and comments wait for the next backfill"
    In this release, edited issues and wiki pages keep the embedding of their *previous* text, and
    comments are not indexed at all, until the backfill below is run again. Because comments are
    searched through the same index, a new comment is not found by **any** search mode, keyword
    included, until then. If your team leans on search, run the backfill on a schedule, for example
    nightly from cron.

## Index existing content

The backfill goes through every issue, wiki page, comment and attachment description, rebuilds
their chunks and embeds them. It is safe to run again whenever you like, and it replaces what was
there, so it also refreshes edited content. Run it after you add the model, after an import, and
whenever the warning above applies.

```bash
docker compose exec api python -m specivo.cli.backfill_embeddings
```

From a source checkout `make backfill-embeddings` does the same, but that target uses the
development compose files, so on a normal install use the command above.

It runs in the foreground and prints progress as it goes. Work is saved in small batches, so
stopping it with Ctrl+C loses very little, and you can start it again. The time it takes grows with
the number of chunks and your CPU. Run a large first backfill when the server is quiet, and raise
`EMBEDDING_ONNX_THREADS` for the duration if you have cores to spare.

!!! note "Run it after the model files are in place"
    If the model files are missing, the backfill still finishes and reports its chunks, but writes no
    embeddings. Add the files and run it again.

An administrator can also start a *fill-the-gaps* backfill for one registered model with
`POST /api/v1/admin/embedding-models/{id}/backfill/`. It runs in the Celery worker (so the worker
must be up) and only adds embeddings for chunks that have none. It does not rebuild chunk text, so
it does not fix stale content. There is no button for it in the web UI.

## Check that it works

1. **Are the files there?**

    ```bash
    docker compose exec api ls -l /app/data/models
    ```

    You should see `multilingual-e5-small.onnx` and `tokenizer.json`.

2. **Does the log agree?** When the model is missing, the `api` logs a warning each time it needs an
   embedding. When it loads, it logs the line below once per process:

    ```bash
    docker compose logs api | grep -iE "embedding model|ONNX model"
    ```

    ```
    Loaded ONNX model multilingual-e5-small from /app/data/models/multilingual-e5-small.onnx (threads=2)
    Local embedding model 'multilingual-e5-small' is not available (missing ONNX/tokenizer files)
    ```

3. **Is anything indexed?** Compare chunks with embeddings. They should be close to equal.

    ```bash
    docker compose exec db psql -U specivo specivo -c \
      "SELECT (SELECT count(*) FROM search_chunks) AS chunks,
              (SELECT count(*) FROM chunk_embeddings) AS embeddings;"
    ```

    (Use your `POSTGRES_USER` and `POSTGRES_DB` if you changed them.) Many chunks and zero embeddings means the chunks were built while the model files were missing.
    Add the files and run the backfill again.

4. **Does a meaning-based search work?** Search for a paraphrase of something you know is in an
   issue, using an [API key](../ai-agents/api-keys.md):

    ```bash
    curl -H "Authorization: Bearer spv_your_api_key_here" \
      "https://your-specivo-host/api/v1/search/?q=sign-in+flow&mode=semantic"
    ```

    Results that do not contain your words mean semantic search is working.

## Changing the model

**Switching to a different model is not supported.** There is no setting for it and no screen for
it, and the one model that runs today is `multilingual-e5-small`.

The pieces that look like they allow it do not add up to a safe procedure:

- Models are rows in the `embedding_models` table, and the search uses the one marked as default.
  An administrator-only REST endpoint can register another row, but registering one does not make it
  the only default, and nothing else in the product manages that table.
- Only the `local` provider runs. The names of hosted providers, and the key field, exist in the
  data model, but no code calls them. Choosing one makes indexing and searching fail.
- A local model is read from `<model name>.onnx` plus a shared `tokenizer.json` in the one `models`
  folder, so two local models would overwrite each other's tokenizer.
- Vectors from different models cannot be compared. After changing the default model every stored
  embedding would have to be rebuilt, and no vector index is created for a new model's size.

Embeddings are stored with whatever size the model produces, so there is no database migration to
resize anything. The practical consequence is for the future: moving to another model will be a full
rebuild of the index, and this page will describe it when it is supported.

Bring-your-own-key hosted embeddings are not available either, whatever older notes say.

## Troubleshooting

**Semantic or hybrid search finds nothing beyond keyword results.**
Work down the list in [Check that it works](#check-that-it-works): the files in
`/app/data/models`, the log line, and the chunks and embeddings counts. The usual causes are no
model files, files in the wrong folder (it must be the `models` folder *inside* the folder mounted
as `/app/data`, and `SPECIVO_DATA_DIR` in `.env` must point at the same place for both containers), or
a backfill that has not run since the files were added.

**A result I know exists is missing, or looks out of date.**
The text may have been edited, or the comment written, since the last backfill. See
[How content is indexed](#how-content-is-indexed).

**`make download-model` fails.**
The script first checks that it can reach `huggingface.co` and stops if not. Behind a proxy or
firewall, allow that host, or download on another machine and copy the files over. See
[Air-gapped install](air-gapped.md#semantic-search-offline). A failed download is removed, and
re-running the script picks up where the completed files end. The five files are listed above if you
would rather fetch them yourself.

**Indexing is slow.**
Indexing is one chunk at a time on the CPU. Raise `EMBEDDING_ONNX_THREADS` while you backfill, and
give the `api` container more cores. Because new issues and wiki pages are embedded inside the
request that creates them, creating many at once (a bulk import through the API, say) is slower than
usual.

**Searching is slow on a large instance.**
Semantic search compares your query with the stored embeddings of the projects you can see. Specivo
does not currently create a vector index for them, so the cost grows with the number of chunks.
Thousands of chunks are fine. Very large instances should expect slower semantic and hybrid queries,
and can fall back to keyword mode.

**The `api` container uses a lot of memory.**
The model is loaded into the process the first time it embeds something and stays loaded. This is
expected: the model file alone is about 370 MB. If memory is tight, give the container more, or leave
the model files out and use keyword search.
