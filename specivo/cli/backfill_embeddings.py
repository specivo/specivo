"""Rebuild the search index: chunk every issue, wiki page, comment and attachment, then embed the chunks.

Chunks are what keyword search reads, and they need no model. Embeddings are
what semantic search reads, and they need the embedding model files in
``data/models``. Without them the command still writes every chunk, says so,
and exits 0; ``--require-model`` makes that an error instead.

Each source is brought in line with the database as it is now:

* issues and wiki pages get their chunks rewritten when the text changed;
* every attachment is indexed (its filename alone is enough), whatever it is
  attached to: an issue, a comment or a wiki page;
* comments follow the current ``SEARCH_INDEX_COMMENTS``,
  ``SEARCH_MIN_COMMENT_LENGTH`` and ``SEARCH_EXCLUDE_BOT_COMMENTS`` settings, so
  chunks of comments those settings now exclude are removed;
* sources whose item no longer exists, or a trashed wiki page, are removed.

Chunks whose text is unchanged keep their embeddings, so the command is cheap
to re-run. ``--reembed`` recomputes the embedding of every chunk, for example
after replacing the model files. Work is committed in small batches, so an
interrupted run loses little.

Exit status: 0 on success (with or without a model), 1 when some sources could
not be indexed or the run failed, 2 when ``--require-model`` is given and no
model can run.

Usage:
    python -m specivo.cli.backfill_embeddings [--reembed] [--require-model]
    # or, from the directory that holds docker-compose.yml:
    make backfill-embeddings ARGS=--reembed
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, delete, func, select, union
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from specivo.models.attachment import Attachment
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.search import ChunkEmbedding, EmbeddingModel, SearchChunk, SearchSource
from specivo.models.wiki import WikiPage
from specivo.schemas.search import SearchSourceType
from specivo.services.search_index_service import describe_error

logger = logging.getLogger("specivo.cli.backfill_embeddings")

#: Sources per committed batch. Embedding is the slow part, so keep it small.
BATCH_SIZE = 20

EXIT_OK = 0
EXIT_ERRORS = 1
EXIT_NO_MODEL = 2

_SOURCE_TYPES = (
    SearchSourceType.ISSUE.value,
    SearchSourceType.WIKI_PAGE.value,
    SearchSourceType.JOURNAL.value,
    SearchSourceType.ATTACHMENT.value,
)
_LABELS = {
    SearchSourceType.ISSUE.value: "issues",
    SearchSourceType.WIKI_PAGE.value: "wiki pages",
    SearchSourceType.JOURNAL.value: "comments",
    SearchSourceType.ATTACHMENT.value: "attachments",
}


@dataclass
class BackfillReport:
    """What a backfill run did."""

    model_name: str | None = None
    model_usable: bool = False
    reembed: bool = False
    sources_chunked: int = 0
    chunks: int = 0
    sources_removed: int = 0
    chunks_embedded: int = 0
    failures: int = 0
    chunks_total: int = 0
    chunks_with_embedding: int = 0

    @property
    def exit_code(self) -> int:
        return EXIT_ERRORS if self.failures else EXIT_OK


def _candidate_ids(source_type: str) -> Select[Any]:
    """Entity ids to visit for *source_type*: every item that could be indexed, plus every indexed one."""
    if source_type == SearchSourceType.ISSUE.value:
        entities = select(Issue.id.label("id"))
    elif source_type == SearchSourceType.WIKI_PAGE.value:
        entities = select(WikiPage.id.label("id"))
    elif source_type == SearchSourceType.JOURNAL.value:
        entities = select(Journal.id.label("id")).where(Journal.notes.isnot(None), Journal.notes != "")
    else:
        entities = select(Attachment.id.label("id"))
    indexed = select(SearchSource.entity_id.label("id")).where(SearchSource.source_type == source_type)
    ids = union(entities, indexed).subquery()
    return select(ids.c.id)


def _sync_source(conn: Connection, source_type: str, entity_id: int) -> tuple[int, bool]:
    """Bring one source in line with the database; return ``(chunk count, removed)``."""
    from specivo.services.search_index_service import load_source_chunks, write_source_chunks

    with conn.begin_nested():
        loaded = load_source_chunks(conn, source_type, entity_id)
        if loaded is None or not loaded[1]:
            result = conn.execute(
                delete(SearchSource).where(SearchSource.source_type == source_type, SearchSource.entity_id == entity_id)
            )
            return 0, bool(result.rowcount)
        project_id, chunks = loaded
        write_source_chunks(conn, source_type, entity_id, project_id, chunks)
        return len(chunks), False


def _sync_in_session(session: Session, source_type: str, entity_id: int) -> tuple[int, bool]:
    return _sync_source(session.connection(), source_type, entity_id)


async def _default_model(session: AsyncSession) -> EmbeddingModel | None:
    from specivo.services.embedding_service import EmbeddingService

    return await EmbeddingService().get_default_model(session)


async def _coverage(session: AsyncSession, model_id: int | None) -> tuple[int, int]:
    total = (await session.execute(select(func.count()).select_from(SearchChunk))).scalar_one()
    if model_id is None:
        return total, 0
    embedded = (
        await session.execute(
            select(func.count()).select_from(ChunkEmbedding).where(ChunkEmbedding.model_id == model_id)
        )
    ).scalar_one()
    return total, embedded


async def run_backfill(
    factory: async_sessionmaker[AsyncSession],
    *,
    reembed: bool = False,
    log: Callable[[str], None] = logger.info,
) -> BackfillReport:
    """Rebuild every source's chunks and embed them when the default model can run."""
    from specivo.services.embedding_service import EmbeddingService

    embedder = EmbeddingService()
    report = BackfillReport(reembed=reembed)

    async with factory() as session:
        model = await _default_model(session)
        model_id = model.id if model is not None else None
        report.model_name = model.name if model is not None else None
        report.model_usable = model is not None and embedder.is_model_usable(model)
        totals = {
            source_type: (
                await session.execute(select(func.count()).select_from(_candidate_ids(source_type).subquery()))
            ).scalar_one()
            for source_type in _SOURCE_TYPES
        }
    log(", ".join(f"{totals[t]} {_LABELS[t]}" for t in _SOURCE_TYPES) + " to check")

    start = time.monotonic()
    for source_type in _SOURCE_TYPES:
        done = 0
        last_id = 0
        while True:
            async with factory() as session:
                ids_query = _candidate_ids(source_type).subquery()
                batch = list(
                    (
                        await session.execute(
                            select(ids_query.c.id)
                            .where(ids_query.c.id > last_id)
                            .order_by(ids_query.c.id)
                            .limit(BATCH_SIZE)
                        )
                    ).scalars()
                )
                if not batch:
                    break
                last_id = batch[-1]

                for entity_id in batch:
                    done += 1
                    try:
                        chunk_count, removed = await session.run_sync(_sync_in_session, source_type, entity_id)
                    except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
                        report.failures += 1
                        logger.warning("  Could not index %s %d: %s", source_type, entity_id, describe_error(exc))
                        logger.debug("Index failure for %s %d", source_type, entity_id, exc_info=True)
                        continue
                    if removed:
                        report.sources_removed += 1
                    if not chunk_count:
                        continue
                    report.sources_chunked += 1
                    report.chunks += chunk_count
                    if not report.model_usable:
                        continue
                    try:
                        # A Core savepoint, like the chunk sync: only Core rows are
                        # written here, so the session holds no state to unwind.
                        conn = await session.connection()
                        async with conn.begin_nested():
                            report.chunks_embedded += await embedder.embed_pending(
                                session, [(source_type, entity_id)], model_id=model_id, reembed=reembed
                            )
                    except Exception as exc:  # noqa: BLE001 - keep the chunks, report the failure
                        report.failures += 1
                        logger.warning("  Could not embed %s %d: %s", source_type, entity_id, describe_error(exc))
                        logger.debug("Embed failure for %s %d", source_type, entity_id, exc_info=True)
                await session.commit()
            if done % (BATCH_SIZE * 5) < BATCH_SIZE or done >= totals[source_type]:
                log(f"  {_LABELS[source_type]}: {done}/{totals[source_type]} ({time.monotonic() - start:.1f}s)")

    async with factory() as session:
        report.chunks_total, report.chunks_with_embedding = await _coverage(session, model_id)
    return report


def summarize(report: BackfillReport, log: Callable[[str], None] = logger.info) -> None:
    """Print what the run did, and say plainly when semantic search stays off."""
    log(
        f"Chunked {report.sources_chunked} sources into {report.chunks} chunks; "
        f"removed {report.sources_removed} stale sources."
    )
    if report.model_usable:
        verb = "Re-embedded" if report.reembed else "Embedded"
        log(
            f"{verb} {report.chunks_embedded} chunks with {report.model_name}; "
            f"{report.chunks_with_embedding} of {report.chunks_total} chunks now have an embedding."
        )
    else:
        reason = (
            "no default embedding model is registered"
            if report.model_name is None
            else f"the files for {report.model_name} are not in data/models"
        )
        logger.warning(
            "WARNING: %s. Only keyword-search chunks were written; 0 chunks were embedded and semantic "
            "search stays off. Download the model (python -m specivo.cli.download_model) and run this "
            "command again.",
            reason[0].upper() + reason[1:],
        )
    if report.failures:
        logger.error("%d sources could not be indexed; see the warnings above.", report.failures)


async def _main_async(reembed: bool, require_model: bool) -> int:
    from specivo.core.database import get_session_factory
    from specivo.services.embedding_service import EmbeddingService

    factory = get_session_factory()
    if require_model:
        async with factory() as session:
            model = await _default_model(session)
        if model is None or not EmbeddingService().is_model_usable(model):
            logger.error(
                "No embedding model can run (%s); nothing was done because --require-model was given.",
                "none is registered" if model is None else f"the files for {model.name} are missing",
            )
            return EXIT_NO_MODEL

    report = await run_backfill(factory, reembed=reembed)
    summarize(report)
    return report.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m specivo.cli.backfill_embeddings",
        description="Rebuild search chunks for all content and embed them with the default model.",
    )
    parser.add_argument(
        "--reembed",
        action="store_true",
        help="Recompute the embedding of every chunk, not only of chunks that have none.",
    )
    parser.add_argument(
        "--require-model",
        action="store_true",
        help="Exit with status 2, without doing anything, when no embedding model can run.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        return asyncio.run(_main_async(args.reembed, args.require_model))
    except Exception as exc:  # pragma: no cover - CLI surface
        logger.error("Backfill failed: %s", describe_error(exc))
        logger.debug("Backfill failure", exc_info=True)
        return EXIT_ERRORS


if __name__ == "__main__":
    sys.exit(main())
