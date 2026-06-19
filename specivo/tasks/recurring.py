"""Recurring-task generation — the Celery beat poller.

A single periodic task processes ALL enabled recurring patterns across every
project, rather than one cron entry per pattern. It is:

- **Idempotent** — :meth:`RecurringPatternService.materialize` diffs against
  already-materialised occurrences (and a partial unique index makes a duplicate
  insert impossible), so a re-run produces no extra issues.
- **Catch-up aware** — the look-ahead window inside ``materialize`` regenerates
  any overdue occurrences a previous run missed (e.g. after downtime).
- **Self-healing** — commit happens per pattern, so a mid-run crash keeps the
  patterns already processed; the next beat tick resumes the rest.
- **Fault-isolated** — one pattern raising during materialisation is logged and
  skipped; it never aborts the run for the remaining patterns.

A Redis distributed lock prevents two workers from generating concurrently.
"""

from __future__ import annotations

import logging
import os

import redis.asyncio as aioredis
from redis.exceptions import LockError, LockNotOwnedError

from specivo.core.constants import CELERY_MAX_RETRIES, CELERY_RETRY_DELAY_RECURRING
from specivo.tasks import celery_app
from specivo.tasks._async import run_async

logger = logging.getLogger(__name__)

_LOCK_KEY = "specivo:generate_recurring_tasks"
_LOCK_TIMEOUT = 600  # seconds — generous: a large backlog may take a while
_LOCK_BLOCKING_TIMEOUT = 5  # seconds — give up quickly if another worker holds it


@celery_app.task(bind=True, max_retries=CELERY_MAX_RETRIES, default_retry_delay=CELERY_RETRY_DELAY_RECURRING)
def generate_recurring_tasks(self) -> None:  # type: ignore[no-untyped-def]
    """Materialise due issues for every enabled recurring pattern.

    Uses a Redis lock to prevent concurrent runs across multiple workers.
    A failed run (e.g. lost DB connection) is retried; individual pattern
    failures are handled inside the run and never trigger a retry.
    """
    try:
        run_async(_generate_recurring_async())
    except Exception as exc:
        logger.warning("Recurring task generation failed: %s", exc)
        raise self.retry(exc=exc)


async def _generate_recurring_async() -> None:
    """Acquire the distributed lock, then run the generation body.

    The lock guards against concurrent generation. If another worker already
    holds it we log and return — the next beat tick will pick up any remaining
    work. The body is factored into :func:`_generate_for_session` so tests can
    drive it directly against a test session without the lock/broker.
    """
    from specivo.core.utils import utcnow
    from specivo.tasks._async import task_session

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    r = aioredis.from_url(redis_url)

    try:
        async with r.lock(_LOCK_KEY, timeout=_LOCK_TIMEOUT, blocking_timeout=_LOCK_BLOCKING_TIMEOUT):
            now = utcnow()
            async with task_session() as session:
                await _generate_for_session(session, now)
    except LockNotOwnedError:
        logger.debug("Recurring task generation skipped — lock lost mid-run")
    except LockError:
        logger.debug("Recurring task generation skipped — another worker holds the lock")
    finally:
        await r.aclose()


async def _generate_for_session(session, now, *, batch_size: int | None = None) -> tuple[int, int, int]:  # type: ignore[no-untyped-def]
    """Materialise due issues for all enabled patterns using *session*.

    Loads patterns in batches of ``batch_size`` (defaults to the configured
    ``recurring_tasks_batch_size``) and materialises each inside its own
    try/except so one failing pattern never aborts the run. Commits per pattern
    so partial progress survives a mid-run crash.

    Returns ``(patterns_processed, total_created, errors)`` for observability
    and testing.

    This is deliberately separate from :func:`_generate_recurring_async` (which
    owns the Redis lock and session lifecycle) so it can be called directly with
    a test session.
    """
    from specivo.core.config import get_settings
    from specivo.core.runtime_settings import get_default_language_override
    from specivo.services.recurring_pattern_service import RecurringPatternService

    if batch_size is None:
        batch_size = get_settings().recurring_tasks_batch_size

    # Workspace language for localizing {{month}} / {{weekday}} template macros.
    # No request context exists in a background job, so use the workspace default.
    locale = get_default_language_override() or get_settings().default_language

    service = RecurringPatternService()

    processed = 0
    total_created = 0
    errors = 0
    offset = 0

    while True:
        batch = await service.list_enabled(session, offset=offset, limit=batch_size)
        if not batch:
            break

        for pattern in batch:
            processed += 1
            # Each pattern runs in its own SAVEPOINT so a failure rolls back
            # only that pattern's partial work, leaving the session clean for
            # the rest of the batch — one failure never poisons the others.
            savepoint = await session.begin_nested()
            try:
                created = await service.materialize(session, pattern, now, locale=locale)
                await savepoint.commit()
                # Commit per pattern: a mid-run crash keeps already-generated
                # issues, and the next beat tick resumes the rest (self-healing).
                await session.commit()
                total_created += len(created)
                if created:
                    logger.info(
                        "Recurring pattern %d (%s): generated %d issue(s)",
                        pattern.id,
                        pattern.name,
                        len(created),
                    )
            except Exception:
                errors += 1
                if savepoint.is_active:
                    await savepoint.rollback()
                logger.exception(
                    "Recurring pattern %d (%s): materialisation failed — skipping",
                    pattern.id,
                    pattern.name,
                )

        if len(batch) < batch_size:
            break
        offset += batch_size

    logger.info(
        "Recurring task generation complete: %d pattern(s) processed, %d issue(s) created, %d error(s)",
        processed,
        total_created,
        errors,
    )
    return processed, total_created, errors
