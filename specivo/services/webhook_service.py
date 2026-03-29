"""WebhookService — manage outgoing webhook configurations and delivery."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import logging
import socket
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError
from specivo.models.webhook import Webhook

logger = logging.getLogger(__name__)

# Private/reserved IP networks that must not be targeted by webhooks (SSRF prevention).
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def _validate_webhook_url(url: str) -> None:
    """Validate that a webhook URL does not target private/loopback/link-local IPs.

    Raises ``ValueError`` if the URL scheme is not http/https or if the
    resolved IP falls within a blocked (private/reserved) network range.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Webhook URL must use http or https scheme, got {parsed.scheme!r}")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Webhook URL must include a hostname")

    try:
        # Resolve hostname to IP addresses
        addr_infos = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve webhook hostname {hostname!r}: {exc}") from exc

    for family, _, _, _, sockaddr in addr_infos:
        ip = ipaddress.ip_address(sockaddr[0])
        for network in _BLOCKED_NETWORKS:
            if ip in network:
                raise ValueError(
                    f"Webhook URL resolves to blocked IP {ip} (network {network}). "
                    f"Webhooks to private/loopback/link-local addresses are not allowed."
                )


def _derive_fernet_key(secret_key: str) -> bytes:
    """Derive a 32-byte Fernet key from the application SECRET_KEY via SHA-256."""
    return base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())


def _encrypt_secret(raw: str, settings) -> str:
    """Encrypt a webhook secret for storage using Fernet (AES-128-CBC).

    The Fernet key is derived from ``settings.secret_key`` so no additional
    configuration is needed.
    """
    f = Fernet(_derive_fernet_key(settings.secret_key))
    return f.encrypt(raw.encode()).decode()


def _decrypt_secret(encrypted: str, settings) -> str:
    """Decrypt a stored webhook secret.

    Falls back to returning the value as-is if decryption fails, which
    handles secrets stored before encryption was introduced.
    """
    try:
        f = Fernet(_derive_fernet_key(settings.secret_key))
        return f.decrypt(encrypted.encode()).decode()
    except Exception:
        # Backward compatibility: secret was stored in plaintext before encryption
        logger.warning("Webhook secret decryption failed — assuming plaintext (pre-encryption)")
        return encrypted


class WebhookService:
    """Stateless service for outgoing webhook management."""

    async def register(
        self,
        session: AsyncSession,
        project_id: int,
        url: str,
        secret: str,
        events: list[str],
    ) -> Webhook:
        """Register a new outgoing webhook for a project."""
        from specivo.core.config import get_settings

        _validate_webhook_url(url)
        webhook = Webhook(
            project_id=project_id,
            url=url,
            secret=_encrypt_secret(secret, get_settings()),
            events=events,
            is_active=True,
        )
        session.add(webhook)
        await session.flush()
        logger.info("Registered webhook %d for project %d -> %s", webhook.id, project_id, url)
        return webhook

    async def list_for_project(self, session: AsyncSession, project_id: int) -> list[Webhook]:
        """List all webhooks for a project."""
        stmt = select(Webhook).where(Webhook.project_id == project_id).order_by(Webhook.created_at.desc())
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def update(
        self,
        session: AsyncSession,
        webhook_id: int,
        project_id: int,
        data: dict,
    ) -> Webhook:
        """Update a webhook's configuration."""
        stmt = select(Webhook).where(Webhook.id == webhook_id, Webhook.project_id == project_id)
        result = await session.execute(stmt)
        webhook = result.scalar_one_or_none()
        if webhook is None:
            raise NotFoundError("Webhook not found")

        if "url" in data:
            _validate_webhook_url(data["url"])
        if "secret" in data:
            from specivo.core.config import get_settings

            data = {**data, "secret": _encrypt_secret(data["secret"], get_settings())}
        for field in ("url", "secret", "events", "is_active"):
            if field in data:
                setattr(webhook, field, data[field])
        await session.flush()
        return webhook

    async def delete(
        self,
        session: AsyncSession,
        webhook_id: int,
        project_id: int,
    ) -> None:
        """Delete a webhook."""
        stmt = select(Webhook).where(Webhook.id == webhook_id, Webhook.project_id == project_id)
        result = await session.execute(stmt)
        webhook = result.scalar_one_or_none()
        if webhook is None:
            raise NotFoundError("Webhook not found")

        await session.execute(delete(Webhook).where(Webhook.id == webhook_id))
        await session.flush()

    async def deliver(
        self,
        session: AsyncSession,
        project_id: int,
        event: str,
        payload: dict,
    ) -> None:
        """Find matching webhooks and queue Celery delivery tasks."""
        stmt = select(Webhook).where(
            Webhook.project_id == project_id,
            Webhook.is_active.is_(True),
        )
        result = await session.execute(stmt)
        webhooks = list(result.scalars().all())

        from specivo.tasks.webhooks import deliver_webhook

        for wh in webhooks:
            if event in wh.events:
                deliver_webhook.delay(wh.id, event, payload)
                logger.info(
                    "Queued webhook delivery: webhook_id=%d event=%s",
                    wh.id,
                    event,
                )
