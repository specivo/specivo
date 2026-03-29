"""Pydantic schemas for Webhook endpoints."""

from __future__ import annotations

import ipaddress
from datetime import datetime
from urllib.parse import urlparse

from pydantic import BaseModel, field_validator

# Private/reserved IP networks to block (SSRF prevention)
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
]


def _is_blocked_ip(host: str) -> bool:
    """Check if a hostname/IP resolves to a blocked network."""
    try:
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in _BLOCKED_NETWORKS)
    except ValueError:
        # Not an IP literal — hostname, allow (DNS resolution happens at delivery)
        return False


class WebhookCreate(BaseModel):
    url: str
    secret: str
    events: list[str]

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("https://"):
            raise ValueError("url must use HTTPS")
        if len(v) > 2048:
            raise ValueError("url must be 2048 characters or fewer")
        # Check for private/loopback IPs in the URL hostname
        parsed = urlparse(v)
        hostname = parsed.hostname or ""
        if _is_blocked_ip(hostname):
            raise ValueError("url must not point to private, loopback, or link-local addresses")
        return v

    @field_validator("secret")
    @classmethod
    def validate_secret(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("secret must be at least 8 characters")
        return v

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("events must contain at least one event type")
        return v


class WebhookUpdate(BaseModel):
    url: str | None = None
    secret: str | None = None
    events: list[str] | None = None
    is_active: bool | None = None


class WebhookOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    project_id: int
    url: str
    events: list[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime
