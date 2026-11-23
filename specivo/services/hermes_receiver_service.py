"""HermesReceiverService — create, list, change and delete Hermes event receivers.

A receiver belongs to one account; people manage their own
(``/my/hermes-receivers/``) and administrators anyone's (Admin > Users).
Creating one mints a URL token and a signing secret, shown once; rotating the
secret mints a new one and the old one stops verifying at once. Every change is
audited (``HERMES_RECEIVER_*``) with ids and names only, never the token or
the secret.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.app_url import base_path, public_url
from specivo.core.exceptions import NotFoundError, ValidationError
from specivo.models.hermes_receiver import HermesReceiver
from specivo.models.user import User
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

if TYPE_CHECKING:
    from fastapi import Request

#: ``secret_box`` purpose of ``hermes_receivers.secret``.
HERMES_RECEIVER_SECRET_PURPOSE = "hermes-receiver-secret"  # noqa: S105

#: The shape of a receiver token (``secrets.token_urlsafe(32)`` is 43 characters).
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,64}")

#: The Hermes events a receiver maps; the ready-made ``hooks.outbound`` block subscribes to these.
SUBSCRIBED_EVENTS = (
    "on_session_start",
    "post_tool_call",
    "pre_approval_request",
    "post_approval_response",
    "subagent_start",
    "subagent_stop",
    "on_session_end",
)

#: The environment variable the ready-made block tells Hermes to read the secret from.
SECRET_ENV = "SPECIVO_WEBHOOK_SECRET"  # noqa: S105

_NAME_MAX = 100
_PLACEHOLDER_HOST = "https://your-specivo-host"

_audit = SecurityAuditService()


def event_path(token: str) -> str:
    """The application path Hermes posts to for receiver *token*."""
    return f"/api/v1/hermes/events/{token}/"


def event_url(token: str) -> str:
    """The receiver's address: ``APP_URL`` based when it is set, else the prefixed path alone."""
    return public_url(event_path(token)) or base_path() + event_path(token)


def hooks_config(token: str) -> str:
    """The ``hooks.outbound`` block for Hermes' ``config.yaml``, ready to paste.

    The secret is not in it: Hermes reads it from ``SECRET_ENV``. Without an
    ``APP_URL`` the host is a placeholder to replace.
    """
    url = event_url(token)
    if not url.startswith(("http://", "https://")):
        url = _PLACEHOLDER_HOST + url
    events = ", ".join(SUBSCRIBED_EVENTS)
    return (
        "hooks:\n"
        "  outbound:\n"
        "    - name: specivo\n"
        f"      url: {url}\n"
        f"      events: [{events}]\n"
        f"      secret_env: {SECRET_ENV}\n"
        "      timeout: 10\n"
    )


def _clean_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValidationError("Name must not be empty", field="name")
    if len(cleaned) > _NAME_MAX:
        raise ValidationError(f"Name must be at most {_NAME_MAX} characters", field="name")
    return cleaned


@dataclass(frozen=True)
class MintedReceiver:
    """A receiver with the credentials that are shown once: its token and secret."""

    receiver: HermesReceiver
    token: str
    secret: str


class HermesReceiverService:
    """Stateless service for Hermes event receivers."""

    async def list_for_user(self, session: AsyncSession, user_id: int) -> list[HermesReceiver]:
        """*user_id*'s receivers, oldest first."""
        stmt = select(HermesReceiver).where(HermesReceiver.user_id == user_id).order_by(HermesReceiver.id)
        return list((await session.execute(stmt)).scalars().all())

    async def get_for_user(self, session: AsyncSession, user_id: int, receiver_id: int) -> HermesReceiver:
        """Receiver *receiver_id* of *user_id*, else ``NotFoundError`` (another account's looks missing)."""
        receiver = await session.get(HermesReceiver, receiver_id)
        if receiver is None or receiver.user_id != user_id:
            raise NotFoundError("Hermes receiver not found")
        return receiver

    async def find_by_token(self, session: AsyncSession, token: str) -> HermesReceiver | None:
        """The active receiver with *token* whose account may still act, else ``None``.

        A deactivated account, a system account and a token of the wrong shape
        all look like an unknown token. A locked account keeps its receivers,
        as it keeps its API keys.
        """
        if not TOKEN_PATTERN.fullmatch(token or ""):
            return None
        stmt = (
            select(HermesReceiver)
            .join(User, User.id == HermesReceiver.user_id)
            .where(
                HermesReceiver.token == token,
                HermesReceiver.is_active.is_(True),
                User.status.in_(("active", "locked")),
                ~User.login.startswith("$"),
            )
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def create(
        self,
        session: AsyncSession,
        *,
        owner: User,
        name: str,
        actor: User,
        request: Request | None = None,
    ) -> MintedReceiver:
        """Create a receiver for *owner*; returns it with its token and secret (shown once)."""
        token = secrets.token_urlsafe(32)
        secret = secrets.token_urlsafe(32)
        receiver = HermesReceiver(
            user_id=owner.id,
            name=_clean_name(name),
            token=token,
            secret=secret_box.encrypt(secret, HERMES_RECEIVER_SECRET_PURPOSE),
            is_active=True,
            created_by_id=actor.id,
        )
        session.add(receiver)
        await session.flush()
        await self._audit(session, AuditEvent.HERMES_RECEIVER_CREATED, receiver, owner, actor, request)
        return MintedReceiver(receiver=receiver, token=token, secret=secret)

    async def update(
        self,
        session: AsyncSession,
        receiver: HermesReceiver,
        *,
        owner: User,
        actor: User,
        name: str | None = None,
        is_active: bool | None = None,
        rotate_secret: bool = False,
        request: Request | None = None,
    ) -> str | None:
        """Change *receiver*; returns the new secret when it was rotated, else ``None``."""
        changes: list[str] = []
        if name is not None:
            cleaned = _clean_name(name)
            if cleaned != receiver.name:
                receiver.name = cleaned
                changes.append("name")
        if is_active is not None and is_active != receiver.is_active:
            receiver.is_active = is_active
            changes.append("is_active")
        new_secret: str | None = None
        if rotate_secret:
            new_secret = secrets.token_urlsafe(32)
            receiver.secret = secret_box.encrypt(new_secret, HERMES_RECEIVER_SECRET_PURPOSE)
            changes.append("secret")
        if changes:
            await session.flush()
            extra: dict[str, object] = {"changes": changes, "secret_rotated": rotate_secret}
            if "is_active" in changes:
                extra["is_active"] = receiver.is_active
            await self._audit(session, AuditEvent.HERMES_RECEIVER_UPDATED, receiver, owner, actor, request, extra)
        return new_secret

    async def delete(
        self,
        session: AsyncSession,
        receiver: HermesReceiver,
        *,
        owner: User,
        actor: User,
        request: Request | None = None,
    ) -> None:
        """Delete *receiver*: its delivery ids go with it, its sessions keep their rows."""
        await self._audit(session, AuditEvent.HERMES_RECEIVER_DELETED, receiver, owner, actor, request)
        await session.execute(delete(HermesReceiver).where(HermesReceiver.id == receiver.id))
        await session.flush()

    @staticmethod
    def read_secret(receiver: HermesReceiver) -> str:
        """The receiver's signing secret; raises ``secret_box.SecretUnreadableError`` after a key change."""
        return secret_box.decrypt(receiver.secret, HERMES_RECEIVER_SECRET_PURPOSE)

    @staticmethod
    async def _audit(
        session: AsyncSession,
        event: AuditEvent,
        receiver: HermesReceiver,
        owner: User,
        actor: User,
        request: Request | None,
        extra: dict[str, object] | None = None,
    ) -> None:
        details: dict[str, object] = {
            "receiver_id": receiver.id,
            "name": receiver.name,
            "target_user_id": owner.id,
            "target_login": owner.login,
        }
        if extra:
            details.update(extra)
        await _audit.log_event(
            session,
            event,
            user_id=actor.id,
            resource_type="hermes_receiver",
            resource_id=receiver.id,
            details=details,
            request=request,
        )
