"""Celery task for delivering outgoing webhooks with HMAC-SHA256 signatures."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid

import httpx

from specivo.core.config import get_settings
from specivo.core.constants import CELERY_MAX_RETRIES, CELERY_RETRY_DELAY_WEBHOOK, WEBHOOK_RESPONSE_MAX_BYTES
from specivo.tasks import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=CELERY_MAX_RETRIES, default_retry_delay=CELERY_RETRY_DELAY_WEBHOOK)
def deliver_webhook(self, webhook_id: int, event: str, payload: dict) -> None:  # type: ignore[no-untyped-def]
    """POST payload to webhook URL with HMAC-SHA256 signature.

    Headers:
    - X-Specivo-Signature: HMAC-SHA256 of the body using the webhook secret
    - X-Specivo-Event: the event type (e.g. "issue.created")
    - X-Specivo-Delivery: unique delivery UUID

    Creates a WebhookDelivery record with status_code and response.
    Retries on 5xx or connection error (up to 3 times).
    """
    import asyncio

    from sqlalchemy import select

    from specivo.tasks._async import task_session

    async def _deliver():
        from specivo.models.webhook import Webhook, WebhookDelivery

        async with task_session() as session:
            result = await session.execute(select(Webhook).where(Webhook.id == webhook_id))
            webhook = result.scalar_one_or_none()
            if webhook is None:
                logger.warning("Webhook %d not found, skipping delivery", webhook_id)
                return

            body = json.dumps(payload, default=str).encode()
            # Decrypt the secret (stored encrypted via Fernet) before signing
            from specivo.services.webhook_service import _decrypt_secret

            raw_secret = _decrypt_secret(webhook.secret, get_settings())
            signature = hmac.new(raw_secret.encode(), body, hashlib.sha256).hexdigest()
            delivery_id = str(uuid.uuid4())

            headers = {
                "Content-Type": "application/json",
                "X-Specivo-Signature": f"sha256={signature}",
                "X-Specivo-Event": event,
                "X-Specivo-Delivery": delivery_id,
            }

            status_code = None
            response_body = None
            success = False

            webhook_timeout = get_settings().webhook_timeout
            try:
                async with httpx.AsyncClient(timeout=webhook_timeout, follow_redirects=False) as client:
                    resp = await client.post(webhook.url, content=body, headers=headers)
                    status_code = resp.status_code
                    response_body = resp.text[:WEBHOOK_RESPONSE_MAX_BYTES]
                    success = 200 <= status_code < 300
            except (httpx.HTTPError, OSError) as exc:
                response_body = str(exc)[:WEBHOOK_RESPONSE_MAX_BYTES]

            delivery = WebhookDelivery(
                webhook_id=webhook_id,
                event=event,
                payload=payload,
                status_code=status_code,
                response_body=response_body,
                attempts=self.request.retries + 1,
                success=success,
            )
            session.add(delivery)
            await session.commit()

            if not success:
                if status_code is not None and status_code >= 500:
                    raise Exception(f"Webhook delivery failed with {status_code}")
                if status_code is None:
                    raise Exception(f"Webhook delivery connection error: {response_body}")

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                pool.submit(lambda: asyncio.run(_deliver())).result()
        else:
            loop.run_until_complete(_deliver())
    except Exception as exc:
        if self.request.retries < self.max_retries:
            logger.warning(
                "Webhook %d delivery failed (attempt %d/%d): %s",
                webhook_id,
                self.request.retries + 1,
                self.max_retries + 1,
                exc,
            )
            raise self.retry(exc=exc)
        logger.error(
            "Webhook %d delivery permanently failed after %d attempts: %s",
            webhook_id,
            self.max_retries + 1,
            exc,
        )
