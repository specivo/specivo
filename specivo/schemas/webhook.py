"""Pydantic schemas for outgoing webhook endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlparse

from pydantic import BaseModel, field_validator

from specivo.core.net_safety import is_blocked_literal

WEBHOOK_SECRET_MIN_LENGTH = 16
WEBHOOK_SECRET_MAX_LENGTH = 100


class WebhookEvent(StrEnum):
    """Events an outgoing webhook can be sent; payloads are described in ``webhook_events``."""

    ISSUE_CREATED = "issue.created"
    ISSUE_UPDATED = "issue.updated"
    ISSUE_COMMENTED = "issue.commented"
    ISSUE_DELETED = "issue.deleted"
    WIKI_PAGE_UPDATED = "wiki.page.updated"
    #: Sent by "Send test"; always delivered, never subscribed to.
    PING = "ping"


SUBSCRIBABLE_EVENTS: tuple[WebhookEvent, ...] = tuple(e for e in WebhookEvent if e is not WebhookEvent.PING)


def _validate_url(v: str) -> str:
    v = v.strip()
    if not v.startswith("https://"):
        raise ValueError("url must use HTTPS")
    if len(v) > 2048:
        raise ValueError("url must be 2048 characters or fewer")
    parsed = urlparse(v)
    hostname = parsed.hostname or ""
    if not hostname:
        raise ValueError("url must include a host name")
    try:
        parsed.port
    except ValueError:
        raise ValueError("url has an invalid port") from None
    # Host names are resolved and checked again at delivery time.
    if is_blocked_literal(hostname):
        raise ValueError("url must not point to private, loopback, or link-local addresses")
    return v


def _validate_events(v: list[WebhookEvent]) -> list[WebhookEvent]:
    if not v:
        raise ValueError("events must contain at least one event type")
    if WebhookEvent.PING in v:
        raise ValueError("'ping' is sent by the test action and cannot be subscribed to")
    return list(dict.fromkeys(v))


def _validate_secret(v: str | None) -> str | None:
    if v is None:
        return v
    if not WEBHOOK_SECRET_MIN_LENGTH <= len(v) <= WEBHOOK_SECRET_MAX_LENGTH:
        raise ValueError(
            f"secret must be {WEBHOOK_SECRET_MIN_LENGTH} to {WEBHOOK_SECRET_MAX_LENGTH} characters; "
            "leave it out to have one generated"
        )
    return v


class WebhookCreate(BaseModel):
    """A new webhook. Leave ``secret`` out to have a random one generated and returned once."""

    url: str
    events: list[WebhookEvent]
    secret: str | None = None
    include_private: bool = False

    _check_url = field_validator("url")(_validate_url)
    _check_events = field_validator("events")(_validate_events)
    _check_secret = field_validator("secret")(_validate_secret)


class WebhookUpdate(BaseModel):
    """Partial update. ``rotate_secret`` replaces the secret with a generated one, returned once."""

    url: str | None = None
    secret: str | None = None
    rotate_secret: bool = False
    events: list[WebhookEvent] | None = None
    is_active: bool | None = None
    include_private: bool | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        return None if v is None else _validate_url(v)

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[WebhookEvent] | None) -> list[WebhookEvent] | None:
        return None if v is None else _validate_events(v)

    _check_secret = field_validator("secret")(_validate_secret)


class WebhookOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    project_id: int
    url: str
    events: list[str]
    is_active: bool
    include_private: bool
    created_at: datetime
    updated_at: datetime


class WebhookSecretOut(WebhookOut):
    """A webhook with its secret in clear: returned only when the secret was just set."""

    secret: str | None = None


class WebhookDeliveryOut(BaseModel):
    """One delivery of an event to a webhook, across its attempts."""

    model_config = {"from_attributes": True}

    id: int
    delivery_uuid: uuid.UUID
    event: str
    status: str
    attempts: int
    status_code: int | None
    response_body: str | None
    last_error: str | None
    next_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime
