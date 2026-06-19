"""Periodic cleanup tasks for expired tokens and stale data.

Tasks:
- cleanup_expired_tokens: Delete expired password reset tokens and refresh tokens.
"""

from __future__ import annotations

import logging
import os

import redis.asyncio as aioredis
from redis.exceptions import LockError, LockNotOwnedError

from specivo.tasks import celery_app
from specivo.tasks._async import run_async

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT = 120  # seconds — max time the lock is held
_LOCK_BLOCKING_TIMEOUT = 5  # seconds — give up quickly if another worker holds the lock


@celery_app.task(bind=True, max_retries=1, default_retry_delay=60)
def cleanup_expired_tokens(self) -> None:  # type: ignore[no-untyped-def]
    """Delete expired password reset tokens and refresh tokens.

    Uses a Redis lock to prevent concurrent runs across multiple workers.
    """
    try:
        run_async(_cleanup_async())
    except Exception as exc:
        logger.warning("Token cleanup failed: %s", exc)
        raise self.retry(exc=exc)


async def _cleanup_async() -> None:
    """Async implementation of token cleanup with Redis distributed lock."""
    from sqlalchemy import delete

    from specivo.core.utils import utcnow
    from specivo.models.auth import PasswordResetToken, RefreshToken
    from specivo.tasks._async import task_session

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    r = aioredis.from_url(redis_url)
    lock_key = "specivo:cleanup_expired_tokens"

    try:
        async with r.lock(lock_key, timeout=_LOCK_TIMEOUT, blocking_timeout=_LOCK_BLOCKING_TIMEOUT):
            now = utcnow()
            async with task_session() as session:
                # Delete expired password reset tokens
                reset_stmt = delete(PasswordResetToken).where(PasswordResetToken.expires_at < now)
                reset_result = await session.execute(reset_stmt)
                reset_count = reset_result.rowcount

                # Delete used password reset tokens older than 7 days
                from datetime import timedelta

                used_cutoff = now - timedelta(days=7)
                used_stmt = delete(PasswordResetToken).where(
                    PasswordResetToken.used_at.is_not(None),
                    PasswordResetToken.used_at < used_cutoff,
                )
                used_result = await session.execute(used_stmt)
                used_count = used_result.rowcount

                # Delete expired refresh tokens
                refresh_stmt = delete(RefreshToken).where(RefreshToken.expires_at < now)
                refresh_result = await session.execute(refresh_stmt)
                refresh_count = refresh_result.rowcount

                await session.commit()

                total = reset_count + used_count + refresh_count
                if total > 0:
                    logger.info(
                        "Token cleanup: deleted %d expired reset, %d used reset, %d expired refresh tokens",
                        reset_count,
                        used_count,
                        refresh_count,
                    )
    except LockNotOwnedError:
        logger.debug("Token cleanup skipped — another worker holds the lock")
    except LockError:
        logger.debug("Token cleanup skipped — could not acquire lock")
    finally:
        await r.aclose()


@celery_app.task(bind=True, max_retries=1, default_retry_delay=60)
def ensure_audit_partitions(self) -> None:  # type: ignore[no-untyped-def]
    """Create monthly partitions for security_audit_logs 3 months ahead."""
    try:
        run_async(_ensure_audit_partitions_async())
    except Exception as exc:
        logger.warning("Audit partition creation failed: %s", exc)
        raise self.retry(exc=exc)


async def _ensure_audit_partitions_async() -> None:
    from specivo.tasks._async import task_session
    from specivo.tasks.partition_management import ensure_partitions

    async with task_session() as session:
        await ensure_partitions(session)
        await session.commit()
