"""Periodic cleanup tasks for expired tokens and stale data.

Tasks:
- cleanup_expired_tokens: Delete expired password reset tokens and refresh tokens,
  and incoming repository and outgoing webhook deliveries past their retention period,
  and Hermes delivery ids older than seven days.
"""

from __future__ import annotations

import logging

from celery.signals import worker_ready

from specivo.core.locks import beat_lock
from specivo.tasks import celery_app
from specivo.tasks._async import run_async

logger = logging.getLogger(__name__)

_LOCK_KEY = "specivo:cleanup_expired_tokens"
_LOCK_TIMEOUT = 120  # seconds — max time the lock is held


@celery_app.task(bind=True, max_retries=1, default_retry_delay=60)
def cleanup_expired_tokens(self) -> None:
    """Delete expired password reset tokens and refresh tokens.

    Uses a Redis lock to prevent concurrent runs across multiple workers.
    """
    try:
        run_async(_cleanup_async())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Token cleanup failed: %s", exc)
        raise self.retry(exc=exc)  # noqa: B904


async def _cleanup_async() -> None:
    """Async implementation of token cleanup with Redis distributed lock."""
    from sqlalchemy import delete

    from specivo.core.utils import utcnow
    from specivo.models.auth import PasswordResetToken, RefreshToken
    from specivo.tasks._async import task_session

    async with beat_lock(_LOCK_KEY, _LOCK_TIMEOUT) as acquired:
        if not acquired:
            logger.debug("Token cleanup skipped — another worker holds the lock")
            return
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

            # Email change links, a week after they expired (used or not)
            from specivo.models.auth import EmailChangeToken

            await session.execute(delete(EmailChangeToken).where(EmailChangeToken.expires_at < now - timedelta(days=7)))

            # Delete expired refresh tokens
            refresh_stmt = delete(RefreshToken).where(RefreshToken.expires_at < now)
            refresh_result = await session.execute(refresh_stmt)
            refresh_count = refresh_result.rowcount

            # Repository webhook delivery log, kept for 30 days
            from specivo.services.repository_hooks import purge_old_deliveries

            delivery_count = await purge_old_deliveries(session)

            # Hermes delivery ids, kept seven days for the redelivery check
            from specivo.services.hermes_event_service import purge_old_deliveries as purge_hermes_deliveries

            hermes_count = await purge_hermes_deliveries(session)

            await session.commit()

            if hermes_count:
                logger.info("Hermes delivery cleanup: deleted %d old delivery ids", hermes_count)

            if delivery_count:
                logger.info("Repository delivery cleanup: deleted %d old deliveries", delivery_count)

            total = reset_count + used_count + refresh_count
            if total > 0:
                logger.info(
                    "Token cleanup: deleted %d expired reset, %d used reset, %d expired refresh tokens",
                    reset_count,
                    used_count,
                    refresh_count,
                )

        # Outgoing webhook delivery log: settled rows only, committed batch by batch
        from specivo.services.webhook_delivery import purge_old_deliveries as purge_old_webhook_deliveries

        async with task_session() as session:
            webhook_count = await purge_old_webhook_deliveries(session)

        if webhook_count:
            logger.info("Webhook delivery cleanup: deleted %d old deliveries", webhook_count)


@celery_app.task(bind=True, max_retries=1, default_retry_delay=60)
def ensure_audit_partitions(self) -> None:
    """Create the audit log's monthly partitions 3 months ahead and drop those past retention.

    Retention is ``AUDIT_LOG_RETENTION_MONTHS`` (default 12, ``0`` keeps everything).
    """
    try:
        run_async(_ensure_audit_partitions_async())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Audit partition maintenance failed: %s", exc)
        raise self.retry(exc=exc)  # noqa: B904


async def _ensure_audit_partitions_async() -> None:
    from specivo.core.config import get_settings
    from specivo.tasks._async import task_session
    from specivo.tasks.partition_management import ensure_partitions, prune_partitions

    async with task_session() as session:
        await ensure_partitions(session)
        await prune_partitions(session, get_settings().audit_log_retention_months)
        await session.commit()


@worker_ready.connect
def _ensure_audit_partitions_at_start(**_kwargs) -> None:
    """Run the partition check once when a worker starts.

    The beat entry first fires a day after start; without this a fresh
    install would write its first audit rows into the default partition.
    """
    try:
        run_async(_ensure_audit_partitions_async())
        logger.info("Audit log partitions checked at worker start")
    except Exception as exc:  # a start-up check must not stop the worker  # noqa: BLE001
        logger.warning("Audit partition check at worker start failed: %s", exc)
