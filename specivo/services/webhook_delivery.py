"""Send one outgoing webhook delivery, and find pending ones the queue lost.

:func:`deliver` makes one attempt at one ``webhook_deliveries`` row. It is run
by the ``deliver_webhook`` Celery task with ``(delivery id, attempts so
far)``; a task whose attempt count no longer matches the row (a duplicate from
the sweeper, or a retry another worker already made) does nothing, and a row
locked by another worker is skipped, so one attempt is sent at a time.

Each attempt resolves the URL's host, refuses it when any address is not
public, and connects to the checked address (the name is kept for the
``Host`` header and for TLS), so a DNS change after the webhook was saved
cannot point it inside the network. Proxy settings from the environment are
ignored for the same reason.

Outcomes: a 2xx response marks the row ``succeeded``. A connection error, a
timeout, a 5xx, 408 or 429 is retried with :func:`backoff_seconds` until
``WEBHOOK_MAX_ATTEMPTS``; anything else (a redirect, another 4xx, a blocked
address, an unreadable secret) marks it ``failed`` at once.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.config import get_settings
from specivo.core.constants import (
    WEBHOOK_ATTEMPT_DEADLINE_SECONDS,
    WEBHOOK_LEASE_SECONDS,
    WEBHOOK_MAX_ATTEMPTS,
    WEBHOOK_RESPONSE_MAX_BYTES,
    WEBHOOK_SWEEP_BATCH,
)
from specivo.core.net_safety import BlockedAddressError, Resolver, UnresolvableHostError, resolve_public_addresses
from specivo.core.utils import utcnow
from specivo.models.webhook import DELIVERY_FAILED, DELIVERY_PENDING, DELIVERY_SUCCEEDED, Webhook, WebhookDelivery
from specivo.schemas.webhook import WebhookEvent
from specivo.services.webhook_events import backoff_seconds, encode_body, signature_header

logger = logging.getLogger(__name__)

WEBHOOK_SECRET_PURPOSE = "webhook-secret"
_RETRYABLE_STATUS = frozenset({408, 429})


@dataclass
class DeliveryOutcome:
    """What an attempt did: the row's new status, and when to try again if it is still pending."""

    status: str
    attempts: int
    retry_in: float | None = None


@dataclass
class _Attempt:
    status_code: int | None = None
    body: str | None = None
    error: str | None = None
    retryable: bool = False

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


def _pinned_url(url: str, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    parts = urlsplit(url)
    host = f"[{ip}]" if ip.version == 6 else str(ip)
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))


async def _read_excerpt(response: httpx.Response) -> str:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= WEBHOOK_RESPONSE_MAX_BYTES:
            break
    text = b"".join(chunks)[:WEBHOOK_RESPONSE_MAX_BYTES].decode("utf-8", errors="replace")
    # PostgreSQL text cannot hold NUL; a receiver must not be able to make the
    # result unsavable (the attempt would roll back and repeat for ever).
    return text.replace("\x00", "")


async def _post(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None,
    resolver: Resolver | None,
) -> _Attempt:
    try:
        return await _post_once(url, body, headers, transport=transport, resolver=resolver)
    except Exception as exc:
        # Anything unforeseen ends the delivery: an attempt that raises would
        # roll back without counting and be retried for ever.
        logger.exception("Webhook delivery to %s failed unexpectedly", urlsplit(url).hostname)
        return _Attempt(error=f"Delivery failed: {type(exc).__name__}.")


async def _post_once(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None,
    resolver: Resolver | None,
) -> _Attempt:
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        port = parts.port or 443
    except ValueError:
        return _Attempt(error="The webhook URL has an invalid port; not sent.")
    try:
        addresses = await resolve_public_addresses(host, port, resolver)
    except BlockedAddressError:
        return _Attempt(error="The host resolves to a private, loopback or reserved address; not sent.")
    except UnresolvableHostError:
        return _Attempt(error=f"Could not resolve host {host}.", retryable=True)

    request_headers = {**headers, "Host": parts.netloc.rsplit("@", 1)[-1]}
    timeout = get_settings().webhook_timeout
    try:
        async with (
            asyncio.timeout(WEBHOOK_ATTEMPT_DEADLINE_SECONDS),
            httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False) as client,
        ):
            # Try each checked address in turn, so a dual-stack host whose
            # first record is unreachable still gets its delivery.
            for index, address in enumerate(addresses):
                request = client.build_request(
                    "POST",
                    _pinned_url(url, address),
                    content=body,
                    headers=request_headers,
                    extensions={"sni_hostname": host},
                )
                try:
                    response = await client.send(request, stream=True)
                except httpx.ConnectError:
                    if index == len(addresses) - 1:
                        raise
                    continue
                try:
                    excerpt = await _read_excerpt(response)
                finally:
                    await response.aclose()
                break
    except httpx.TimeoutException:
        return _Attempt(error=f"Timed out after {timeout} seconds.", retryable=True)
    except TimeoutError:
        return _Attempt(
            error=f"Timed out: no full answer within {WEBHOOK_ATTEMPT_DEADLINE_SECONDS} seconds.", retryable=True
        )
    except (httpx.HTTPError, OSError) as exc:
        return _Attempt(error=f"Connection failed: {type(exc).__name__}.", retryable=True)

    code = response.status_code
    attempt = _Attempt(status_code=code, body=excerpt)
    if not attempt.ok:
        attempt.retryable = code >= 500 or code in _RETRYABLE_STATUS
        attempt.error = f"Receiver answered HTTP {code}."
    return attempt


async def deliver(
    session: AsyncSession,
    delivery_id: int,
    attempts: int,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
    rand: Callable[[], float] = random.random,
) -> DeliveryOutcome | None:
    """Make attempt ``attempts + 1`` at a delivery and commit the result.

    Returns None when there is nothing to do: the row is gone, settled, locked
    by another worker, or already past this attempt.
    """
    row = (
        await session.execute(
            select(WebhookDelivery).where(WebhookDelivery.id == delivery_id).with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if row is None or row.status != DELIVERY_PENDING or row.attempts != attempts:
        await session.rollback()
        return None
    webhook = await session.get(Webhook, row.webhook_id)
    if webhook is None:
        await session.rollback()
        return None

    result: _Attempt
    if not webhook.is_active and row.event != WebhookEvent.PING:
        result = _Attempt(error="The webhook is disabled; not sent.")
    else:
        try:
            secret = secret_box.decrypt(webhook.secret, WEBHOOK_SECRET_PURPOSE)
        except secret_box.SecretUnreadableError:
            result = _Attempt(error="The webhook secret cannot be read with the current SECRET_KEY; set a new one.")
        else:
            body = encode_body(row.payload)
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "Specivo-Webhook/1",
                "X-Specivo-Event": row.event,
                "X-Specivo-Delivery": str(row.delivery_uuid),
                "X-Specivo-Signature": signature_header(secret, body),
            }
            result = await _post(webhook.url, body, headers, transport=transport, resolver=resolver)

    now = utcnow()
    row.attempts += 1
    row.status_code = result.status_code
    row.response_body = result.body
    row.last_error = result.error
    outcome = DeliveryOutcome(status=row.status, attempts=row.attempts)
    if result.ok:
        row.status = outcome.status = DELIVERY_SUCCEEDED
        row.next_attempt_at = None
    elif result.retryable and row.attempts < WEBHOOK_MAX_ATTEMPTS:
        outcome.retry_in = backoff_seconds(row.attempts, rand)
        row.next_attempt_at = now + timedelta(seconds=outcome.retry_in)
    else:
        row.status = outcome.status = DELIVERY_FAILED
        row.next_attempt_at = None
    await session.commit()

    logger.info(
        "Webhook delivery %d (webhook %d, %s) attempt %d: %s%s",
        row.id,
        row.webhook_id,
        row.event,
        row.attempts,
        outcome.status,
        f", HTTP {result.status_code}" if result.status_code is not None else "",
    )
    return outcome


async def claim_due(session: AsyncSession, now: datetime | None = None) -> list[tuple[int, int]]:
    """Take pending deliveries whose ``next_attempt_at`` has passed and lease them again.

    Returns ``(delivery id, attempts so far)`` for the caller to queue, and
    commits. Pushing ``next_attempt_at`` a lease ahead keeps the next sweep
    from queuing them a second time while they wait in the queue.
    """
    now = now or utcnow()
    rows = (
        await session.execute(
            select(WebhookDelivery.id, WebhookDelivery.attempts)
            .where(WebhookDelivery.status == DELIVERY_PENDING, WebhookDelivery.next_attempt_at <= now)
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(WEBHOOK_SWEEP_BATCH)
            .with_for_update(skip_locked=True)
        )
    ).all()
    if rows:
        await session.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.id.in_([r.id for r in rows]))
            .values(next_attempt_at=now + timedelta(seconds=WEBHOOK_LEASE_SECONDS))
        )
    await session.commit()
    return [(r.id, r.attempts) for r in rows]
