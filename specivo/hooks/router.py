"""Incoming forge webhooks: ``POST /hooks/{provider}/{public_id}/``.

One URL per repository integration. In order:

1. A body over ``GIT_HOOK_MAX_BODY_BYTES`` is refused with 413, by its
   ``Content-Length`` and again while reading.
2. An unknown provider, an unknown or malformed id and a disabled integration
   all answer the same 404.
3. Rate limits: per integration, and per client address for ids that match
   nothing. 429.
4. The signature or token is checked on the raw bytes, before any parsing.
   401, logged with the integration id and client address only.
5. The delivery is recorded once (``ON CONFLICT DO NOTHING``); a delivery id
   seen before answers 200 ``duplicate`` and changes nothing.
6. A payload that names another repository is ``ignored``; ``ping`` is
   answered at once; a push is committed as ``accepted``, queued for the
   Celery task, and answered 202, so a large push never makes the forge wait.

The route is exempt from CSRF (``_CSRF_EXEMPT_PATTERNS``): forges call it
server to server and the signature is the proof.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.client_ip import get_client_ip, rate_limit_subject
from specivo.core.config import get_settings
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import AppError
from specivo.core.rate_limit import enforce_rate_limit
from specivo.hooks.providers import PROVIDER_NAMES, ForgeProvider, SignatureError, get_provider
from specivo.models.repository import (
    DELIVERY_ACCEPTED,
    DELIVERY_DUPLICATE,
    DELIVERY_FAILED,
    DELIVERY_IGNORED,
    DELIVERY_PROCESSED,
    RepositoryIntegration,
)
from specivo.services.repository_hooks import find_integration, queue_processing, record_delivery
from specivo.services.repository_service import REPO_INTEGRATION_SECRET_PURPOSE

logger = logging.getLogger(__name__)

hooks_router = APIRouter(prefix="/hooks", tags=["hooks"])

_HANDLED_EVENTS = ("push", "ping")


def _not_found() -> AppError:
    # Identical for every reason, so the answer does not say which ids exist.
    return AppError(code="not_found", message="Not found", status_code=404)


def _too_large() -> AppError:
    return AppError(code="payload_too_large", message="Payload too large", status_code=413)


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise _too_large()
        except ValueError:
            raise AppError(code="bad_request", message="Invalid Content-Length", status_code=400) from None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


async def _lookup(db: AsyncSession, provider_name: str, public_id: str) -> RepositoryIntegration | None:
    if provider_name not in PROVIDER_NAMES:
        return None
    try:
        parsed = uuid.UUID(public_id)
    except ValueError:
        return None
    return await find_integration(db, provider_name, parsed)


def _verify(provider: ForgeProvider, integration: RepositoryIntegration, request: Request, body: bytes) -> None:
    client = get_client_ip(request) or "unknown"
    try:
        secret = secret_box.decrypt(integration.secret_encrypted, REPO_INTEGRATION_SECRET_PURPOSE)
    except secret_box.SecretUnreadableError:
        logger.warning(
            "Repository hook secret unreadable (SECRET_KEY changed?): integration %d; rotate the secret",
            integration.id,
            extra={"integration_id": integration.id, "client_ip": client, "outcome": "unauthorized"},
        )
        raise AppError(code="webhook_unauthorized", message="Invalid signature", status_code=401) from None
    try:
        provider.verify(request.headers, body, secret)
    except SignatureError as exc:
        logger.warning(
            "Repository hook signature refused: integration %d from %s (%s)",
            integration.id,
            client,
            exc,
            extra={"integration_id": integration.id, "client_ip": client, "outcome": "unauthorized"},
        )
        raise AppError(code="webhook_unauthorized", message="Invalid signature", status_code=401) from None


def _classify(
    provider: ForgeProvider, integration: RepositoryIntegration, event: str, payload: Any
) -> tuple[str, str | None]:
    """The status a new delivery is recorded with, and why."""
    if not isinstance(payload, dict):
        return DELIVERY_FAILED, "The body is not a JSON object."
    repo = provider.repo_full_name(payload)
    if repo is None or repo.lower() != integration.repo_full_name.lower():
        return DELIVERY_IGNORED, f"The payload names repository {repo or '(none)'}, not {integration.repo_full_name}."
    if event == "ping":
        return DELIVERY_PROCESSED, "Ping received."
    if event == "push":
        return DELIVERY_ACCEPTED, None
    return DELIVERY_IGNORED, f"Event {event} is not handled."


@hooks_router.post("/{provider_name}/{public_id}/")
async def receive_repository_webhook(
    provider_name: str,
    public_id: str,
    request: Request,
    response: Response,
    db: AsyncSession = DB_SESSION,
) -> JSONResponse:
    """Receive a webhook from GitHub, GitLab, Gitea or Forgejo for one repository integration."""
    settings = get_settings()
    body = await _read_body(request, settings.git_hook_max_body_bytes)

    integration = await _lookup(db, provider_name, public_id)
    if integration is None:
        subject = rate_limit_subject(get_client_ip(request) or "unknown")
        await enforce_rate_limit(
            request,
            response,
            "git_hook_unknown",
            settings.git_hook_unknown_rate_limit_per_minute,
            60,
            identifier=subject,
        )
        raise _not_found()
    provider = get_provider(provider_name)
    try:
        _verify(provider, integration, request, body)
    except AppError:
        # Bad signatures spend the sender's IP bucket, not the integration's,
        # so someone who learned the URL cannot starve the real forge.
        await enforce_rate_limit(
            request,
            response,
            "git_hook_unknown",
            settings.git_hook_unknown_rate_limit_per_minute,
            60,
            identifier=rate_limit_subject(get_client_ip(request) or "unknown"),
        )
        raise
    await enforce_rate_limit(
        request, response, "git_hook", settings.git_hook_rate_limit_per_minute, 60, identifier=str(integration.id)
    )

    event = provider.event_type(request.headers) or "unknown"
    delivery_id = provider.delivery_id(request.headers, body)
    try:
        payload: Any = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        payload = None
    status, detail = _classify(provider, integration, event, payload)
    integration_id = integration.id

    row_id = await record_delivery(
        db,
        integration,
        delivery_id=delivery_id,
        event=event,
        status=status,
        payload=payload if isinstance(payload, dict) else None,
        detail=detail,
    )
    log_extra = {"integration_id": integration_id, "delivery_id": delivery_id, "event": event}
    if row_id is None:
        logger.info(
            "Repository hook duplicate delivery %s", delivery_id, extra={**log_extra, "outcome": DELIVERY_DUPLICATE}
        )
        return JSONResponse({"status": DELIVERY_DUPLICATE}, status_code=200)
    await db.commit()
    logger.info("Repository hook delivery %s %s", delivery_id, status, extra={**log_extra, "outcome": status})

    if status == DELIVERY_FAILED:
        return JSONResponse({"status": status, "detail": detail}, status_code=400)
    if status == DELIVERY_ACCEPTED:
        queue_processing([row_id])
        return JSONResponse({"status": status}, status_code=202)
    return JSONResponse({"status": status}, status_code=200)
