"""Hermes event receiver: ``POST /api/v1/hermes/events/{token}/`` for Hermes Agent's outbound webhooks.

Hermes sends no ``Authorization`` header: the token in the URL names the
receiver and the HMAC-SHA256 signature over the raw body proves the sender.
Hermes retries once on a connection error or a 5xx and treats every 4xx as
final, so every refusal below is a 4xx and a misconfigured sender produces one
refused delivery per event, not a retry storm. In order:

1. A body over ``HERMES_EVENT_MAX_BODY_BYTES`` (by ``Content-Length``, then
   while reading) is 413 ``payload_too_large``.
2. Requests per minute per client address over
   ``HERMES_EVENT_RATE_LIMIT_PER_MINUTE`` are 429.
3. An unknown or inactive token, or one whose account cannot act, is 404,
   audited as ``hermes_event_rejected`` at most once a minute per address.
4. A missing or wrong ``X-Hermes-Signature-256`` is 401 ``invalid_signature``,
   audited once per delivery.
5. A body that is not a JSON object, or lacks a usable ``delivery_id``, is 400.
   A ``timestamp`` missing or more than ten minutes from the server clock is
   401 ``stale`` (audited).
6. A ``delivery_id`` seen before is 200 ``duplicate`` and writes nothing.
7. A mapped event is 200 ``accepted`` with its agent session; any other
   verified event is 200 ``ignored``.

``GET`` on the same URL answers ``{"ok": true, "receiver": name}`` for the
Hermes plugin's ``doctor`` check. Both are served with and without the
trailing slash (the bare form hidden from OpenAPI): Hermes refuses redirects.
No secret, token or payload content is ever logged.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from specivo.core import secret_box
from specivo.core.client_ip import get_client_ip, rate_limit_subject
from specivo.core.config import get_settings
from specivo.core.database import DbSession
from specivo.core.exceptions import AppError
from specivo.core.rate_limit import enforce_rate_limit
from specivo.core.security import first_auth_failure_in_window
from specivo.models.hermes_receiver import HermesReceiver
from specivo.schemas.hermes import HermesEventAccepted, HermesReceiverPing
from specivo.services.hermes_event_service import (
    STATUS_DUPLICATE,
    HermesEventService,
    check_timestamp,
    delivery_id_of,
    verify_signature,
)
from specivo.services.hermes_receiver_service import HermesReceiverService
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hermes/events", tags=["hermes"])
_receivers = HermesReceiverService()
_events = HermesEventService()
_audit = SecurityAuditService()

_SIGNATURE_HEADER = "x-hermes-signature-256"
_EVENT_HEADER = "x-hermes-event"


def _not_found() -> AppError:
    # The same answer for every reason, so it does not say which tokens exist.
    return AppError(code="not_found", message="Not found", status_code=404)


def _too_large() -> AppError:
    return AppError(code="payload_too_large", message="Payload too large", status_code=413)


async def _rate_limit(request: Request, response: Response) -> None:
    subject = rate_limit_subject(get_client_ip(request) or "unknown")
    limit = get_settings().hermes_event_rate_limit_per_minute
    await enforce_rate_limit(request, response, "hermes_events", limit, 60, identifier=subject)


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


async def _reject(db: DbSession, request: Request, reason: str, receiver: HermesReceiver | None = None) -> None:
    """Audit a refused delivery (buffered by the request, so it survives the error answer)."""
    details: dict[str, Any] = {"reason": reason, "event": (request.headers.get(_EVENT_HEADER) or "")[:64] or None}
    if receiver is not None:
        details["receiver_id"] = receiver.id
    try:
        await _audit.log_event(
            db,
            AuditEvent.HERMES_EVENT_REJECTED,
            user_id=receiver.user_id if receiver is not None else None,
            resource_type="hermes_receiver",
            resource_id=receiver.id if receiver is not None else None,
            details=details,
            request=request,
        )
    except Exception:  # an audit write never breaks the answer
        logger.warning("Hermes event rejection audit failed", exc_info=True)


async def _receiver_or_404(db: DbSession, request: Request, token: str) -> HermesReceiver:
    receiver = await _receivers.find_by_token(db, token)
    if receiver is None:
        if await first_auth_failure_in_window(get_client_ip(request), "hermes_unknown_receiver"):
            await _reject(db, request, "unknown_receiver")
        raise _not_found()
    return receiver


async def _verify(db: DbSession, request: Request, receiver: HermesReceiver, body: bytes) -> None:
    header = request.headers.get(_SIGNATURE_HEADER)
    if not header:
        await _reject(db, request, "missing_signature", receiver)
        raise AppError(code="invalid_signature", message="Missing signature", status_code=401)
    try:
        secret = _receivers.read_secret(receiver)
    except secret_box.SecretUnreadableError:
        logger.warning("Hermes receiver %d secret unreadable (SECRET_KEY changed?); rotate the secret", receiver.id)
        await _reject(db, request, "secret_unreadable", receiver)
        raise AppError(code="invalid_signature", message="Invalid signature", status_code=401) from None
    if not verify_signature(secret, body, header):
        logger.info("Hermes event signature refused: receiver %d", receiver.id)
        await _reject(db, request, "invalid_signature", receiver)
        raise AppError(code="invalid_signature", message="Invalid signature", status_code=401)


def _bad_payload(message: str, code: str = "invalid_payload") -> AppError:
    return AppError(code=code, message=message, status_code=400)


@router.get("/{token}", response_model=HermesReceiverPing, include_in_schema=False)
@router.get("/{token}/", response_model=HermesReceiverPing, summary="Check a Hermes receiver URL")
async def ping_receiver(token: str, request: Request, response: Response, db: DbSession) -> HermesReceiverPing:
    """Answer whether the URL names an active receiver, with its name. No signature is involved."""
    await _rate_limit(request, response)
    receiver = await _receiver_or_404(db, request, token)
    return HermesReceiverPing(ok=True, receiver=receiver.name)


@router.post("/{token}", response_model=HermesEventAccepted, include_in_schema=False)
@router.post(
    "/{token}/",
    response_model=HermesEventAccepted,
    summary="Receive a Hermes outbound webhook",
    responses={
        400: {"description": "Not a JSON object, or no usable delivery_id"},
        401: {"description": "Missing or wrong signature (invalid_signature), or a stale timestamp (stale)"},
        404: {"description": "Unknown or inactive receiver"},
        413: {"description": "Body too large"},
        429: {"description": "Too many requests from this address"},
    },
)
async def receive_event(token: str, request: Request, response: Response, db: DbSession) -> JSONResponse:
    """Verify one Hermes event and record it against the receiver's account (see the module docstring)."""
    limit = get_settings().hermes_event_max_body_bytes
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise _too_large()
    await _rate_limit(request, response)
    body = await _read_body(request, limit)

    receiver = await _receiver_or_404(db, request, token)
    await _verify(db, request, receiver, body)

    try:
        payload: Any = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _bad_payload("The body is not valid JSON", code="invalid_json") from None
    if not isinstance(payload, dict):
        raise _bad_payload("The body is not a JSON object")
    if not check_timestamp(payload.get("timestamp")):
        await _reject(db, request, "stale", receiver)
        raise AppError(code="stale", message="The event timestamp is missing or too far from now", status_code=401)
    delivery_id = delivery_id_of(payload)
    if delivery_id is None:
        raise _bad_payload("The body has no usable delivery_id")

    if not await _events.record_delivery(db, receiver, delivery_id):
        return JSONResponse({"status": STATUS_DUPLICATE}, status_code=200)
    result = await _events.apply(db, receiver, payload, delivery_id)
    out = HermesEventAccepted(status=result.status, agent_session_id=result.agent_session_id)
    return JSONResponse(out.model_dump(exclude_none=True), status_code=200)
