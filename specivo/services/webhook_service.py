"""WebhookService — manage outgoing webhook configurations and read their delivery log.

Secrets are stored as ``secret_box`` tokens (purpose ``webhook-secret``) and
returned in clear only by the call that set them. Every change to a project's
webhooks drops its cached subscription list (see
:mod:`specivo.services.webhook_events`).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.exceptions import NotFoundError, ValidationError
from specivo.core.net_safety import is_blocked_literal
from specivo.models.user import User
from specivo.models.webhook import Webhook, WebhookDelivery
from specivo.schemas.webhook import WebhookEvent
from specivo.services.webhook_delivery import WEBHOOK_SECRET_PURPOSE
from specivo.services.webhook_events import emit_to_webhook, generate_secret, invalidate_subscriptions

logger = logging.getLogger(__name__)


def _validate_webhook_url(url: str) -> None:
    """Refuse a URL that is not http(s) or names a non-public IP address.

    A host name is not resolved here: it is resolved and checked on every
    delivery, which is the check that matters (DNS can change).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValidationError(f"Webhook URL must use http or https scheme, got {parsed.scheme!r}", field="url")
    if not parsed.hostname:
        raise ValidationError("Webhook URL must include a hostname", field="url")
    try:
        parsed.port
    except ValueError:
        raise ValidationError("Webhook URL has an invalid port", field="url") from None
    if is_blocked_literal(parsed.hostname):
        raise ValidationError(
            "Webhooks to private, loopback or link-local addresses are not allowed.",
            field="url",
        )


class WebhookService:
    """Stateless service for outgoing webhook management."""

    async def register(
        self,
        session: AsyncSession,
        project_id: int,
        url: str,
        events: Sequence[str],
        secret: str | None = None,
        include_private: bool = False,
    ) -> tuple[Webhook, str]:
        """Register a webhook; returns it and its secret in clear (generated when *secret* is None)."""
        _validate_webhook_url(url)
        raw_secret = secret or generate_secret()
        webhook = Webhook(
            project_id=project_id,
            url=url,
            secret=secret_box.encrypt(raw_secret, WEBHOOK_SECRET_PURPOSE),
            events=[str(e) for e in events],
            is_active=True,
            include_private=include_private,
        )
        session.add(webhook)
        await session.flush()
        await invalidate_subscriptions(session, project_id)
        logger.info("Registered webhook %d for project %d", webhook.id, project_id)
        return webhook, raw_secret

    async def list_for_project(self, session: AsyncSession, project_id: int) -> list[Webhook]:
        """List all webhooks for a project."""
        stmt = select(Webhook).where(Webhook.project_id == project_id).order_by(Webhook.created_at.desc())
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get(self, session: AsyncSession, webhook_id: int, project_id: int) -> Webhook:
        """One webhook of the project; NotFoundError for another project's."""
        stmt = select(Webhook).where(Webhook.id == webhook_id, Webhook.project_id == project_id)
        webhook = (await session.execute(stmt)).scalar_one_or_none()
        if webhook is None:
            raise NotFoundError("Webhook not found")
        return webhook

    async def update(
        self,
        session: AsyncSession,
        webhook_id: int,
        project_id: int,
        data: dict[str, Any],
    ) -> tuple[Webhook, str | None]:
        """Update a webhook; returns it and the new secret in clear when one was set."""
        webhook = await self.get(session, webhook_id, project_id)

        new_secret: str | None = None
        if data.get("rotate_secret"):
            new_secret = generate_secret()
        elif data.get("secret"):
            new_secret = data["secret"]
        if new_secret is not None:
            webhook.secret = secret_box.encrypt(new_secret, WEBHOOK_SECRET_PURPOSE)

        if data.get("url") is not None:
            _validate_webhook_url(data["url"])
            webhook.url = data["url"]
        if data.get("events") is not None:
            webhook.events = [str(e) for e in data["events"]]
        for field in ("is_active", "include_private"):
            if data.get(field) is not None:
                setattr(webhook, field, data[field])
        await session.flush()
        await invalidate_subscriptions(session, project_id)
        return webhook, new_secret

    async def delete(
        self,
        session: AsyncSession,
        webhook_id: int,
        project_id: int,
    ) -> None:
        """Delete a webhook with its delivery log."""
        await self.get(session, webhook_id, project_id)
        await session.execute(delete(Webhook).where(Webhook.id == webhook_id))
        await session.flush()
        await invalidate_subscriptions(session, project_id)

    async def list_deliveries(
        self,
        session: AsyncSession,
        webhook: Webhook,
        offset: int = 0,
        limit: int = 25,
    ) -> tuple[list[WebhookDelivery], int]:
        """The webhook's deliveries, newest first, and their total count."""
        base = select(WebhookDelivery).where(WebhookDelivery.webhook_id == webhook.id)
        total = await session.scalar(select(func.count()).select_from(base.subquery()))
        rows = await session.execute(
            base.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc()).offset(offset).limit(limit)
        )
        return list(rows.scalars().all()), int(total or 0)

    async def send_test(self, session: AsyncSession, webhook: Webhook, actor: User) -> WebhookDelivery:
        """Queue a ``ping`` delivery to *webhook*, sent after the transaction commits."""
        return await emit_to_webhook(
            session,
            webhook,
            WebhookEvent.PING,
            actor,
            {"webhook_id": webhook.id, "message": "Test event sent from Specivo."},
        )
