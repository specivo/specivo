"""Schemas of Hermes event receivers (``/my/hermes-receivers/``, ``/admin/users/{id}/hermes-receivers/``)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class HermesReceiverCreate(BaseModel):
    """A new receiver: a name to tell it apart (for example the Hermes host or profile)."""

    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be empty")
        return value


class HermesReceiverUpdate(BaseModel):
    """A change: rename, switch on or off, or rotate the secret (the new one is answered once)."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None
    rotate_secret: bool = False


class HermesReceiverOut(BaseModel):
    """A receiver without its credentials: neither the token nor the secret is ever read back."""

    model_config = {"from_attributes": True}

    id: int
    user_id: int
    name: str
    is_active: bool
    last_event_at: datetime | None
    created_at: datetime
    created_by_id: int | None


class HermesReceiverCreated(HermesReceiverOut):
    """A receiver just created, with what is shown only now: token, secret, URL and the Hermes block."""

    token: str
    secret: str
    url: str
    secret_env: str
    hooks_config: str


class HermesReceiverPatched(HermesReceiverOut):
    """A receiver after a change; ``secret`` is the new secret when it was rotated, shown once."""

    secret: str | None = None
    secret_env: str | None = None


class HermesEventAccepted(BaseModel):
    """The answer to a delivery Hermes need not retry."""

    status: str
    agent_session_id: int | None = None


class HermesReceiverPing(BaseModel):
    """The answer to ``GET`` on a receiver URL (the plugin's ``doctor`` check)."""

    ok: bool
    receiver: str
