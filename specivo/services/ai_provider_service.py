"""A project's AI provider: set, read and remove it, and build the client that calls it.

Only an instance administrator sets or removes a provider: it tells the server
where to send a project's content, and with which key, so it is the same kind
of decision as the instance's own outbound connections. Reading it (without
the key) is for whoever manages the project.

The URL is checked for shape only (``http`` or ``https``, a host, no user
info, query or fragment): pointing it at a model on the local network is the
point of air-gapped installs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.ai_pricing import normalize_provider
from specivo.core.exceptions import ValidationError
from specivo.models.ai_provider import AI_PROVIDER_SECRET_PURPOSE, ProjectAiProvider
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.llm_client import OpenAICompatibleClient

logger = logging.getLogger(__name__)

_PROVIDER_MAX = 50
_MODEL_MAX = 200
_URL_MAX = 500
_KEY_MAX = 1000


@dataclass(slots=True)
class ProviderInput:
    """A provider as an administrator enters it. ``api_key``: None keeps the stored key, "" removes it."""

    base_url: str
    model: str
    provider: str = "openai"
    api_key: str | None = None


def check_base_url(value: str) -> str:
    """The endpoint base URL, without a trailing slash; 422 for anything but a plain http(s) URL."""
    url = (value or "").strip()
    if not url or len(url) > _URL_MAX:
        raise ValidationError(f"Enter the provider's base URL, at most {_URL_MAX} characters", field="base_url")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValidationError(
            "The base URL starts with http:// or https:// and names a host, for example https://api.openai.com/v1",
            field="base_url",
        )
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValidationError(
            "The base URL must not hold a user name, password, query or fragment; enter the key in its own field",
            field="base_url",
        )
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise ValidationError("The base URL must not contain spaces or control characters", field="base_url")
    return url.rstrip("/")


def _check_text(value: str, field: str, max_length: int, message: str) -> str:
    text = (value or "").strip()
    if not text or len(text) > max_length or any(ord(ch) < 32 for ch in text):
        raise ValidationError(message, field=field)
    return text


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port


class AiProviderService:
    async def get(self, session: AsyncSession, project_id: int) -> ProjectAiProvider | None:
        return await session.scalar(select(ProjectAiProvider).where(ProjectAiProvider.project_id == project_id))

    async def set(self, session: AsyncSession, project: Project, data: ProviderInput, actor: User) -> ProjectAiProvider:
        """Create or replace the project's provider (the caller checked that *actor* is an administrator)."""
        base_url = check_base_url(data.base_url)
        model = _check_text(data.model, "model", _MODEL_MAX, f"Enter the model name, at most {_MODEL_MAX} characters")
        provider = normalize_provider(
            _check_text(
                data.provider, "provider", _PROVIDER_MAX, f"Enter a provider name, at most {_PROVIDER_MAX} characters"
            )
        )
        row = await self.get(session, project.id)
        if row is not None and data.api_key is None and _origin(row.base_url) != _origin(base_url):
            # A key is never sent to another server than the one it was entered for.
            row.api_key_encrypted = None
        if row is None:
            row = ProjectAiProvider(
                project_id=project.id, provider=provider or "openai", base_url=base_url, model=model
            )
            session.add(row)
        row.provider = provider or "openai"
        row.base_url = base_url
        row.model = model
        row.updated_by_id = actor.id
        if data.api_key is not None:
            key = data.api_key.strip()
            if len(key) > _KEY_MAX or any(ord(ch) < 32 for ch in key):
                raise ValidationError(f"The API key is at most {_KEY_MAX} printable characters", field="api_key")
            row.api_key_encrypted = secret_box.encrypt(key, AI_PROVIDER_SECRET_PURPOSE) if key else None
        await session.flush()
        return row

    async def delete(self, session: AsyncSession, project: Project) -> bool:
        """Remove the project's provider and switch concept extraction off; whether there was one."""
        from specivo.services.concept_extraction import ENABLED_SETTING

        row = await self.get(session, project.id)
        if row is None:
            return False
        await session.delete(row)
        if (project.settings or {}).get(ENABLED_SETTING):
            project.settings = {**(project.settings or {}), ENABLED_SETTING: False}
        await session.flush()
        return True

    @staticmethod
    def describe(row: ProjectAiProvider | None, *, full_url: bool = True) -> dict[str, Any] | None:
        """The provider as the API shows it: never the key, only whether there is one.

        Without *full_url* the base URL is cut to its scheme and host (no path, which may hold a token).
        """
        if row is None:
            return None
        updated: datetime | None = row.updated_at
        parts = urlsplit(row.base_url)
        return {
            "provider": row.provider,
            "base_url": row.base_url if full_url else f"{parts.scheme}://{parts.hostname}",
            "model": row.model,
            "has_api_key": row.api_key_encrypted is not None,
            "updated_at": updated,
        }

    @staticmethod
    def client_for(row: ProjectAiProvider) -> OpenAICompatibleClient:
        """The client that calls *row*'s endpoint; raises ``SecretUnreadableError`` for an unreadable key."""
        key = secret_box.decrypt(row.api_key_encrypted, AI_PROVIDER_SECRET_PURPOSE) if row.api_key_encrypted else None
        return OpenAICompatibleClient(row.base_url, row.model, key)
