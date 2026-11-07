"""AI usage ingest API, contract v1 (``docs/contracts/ai-usage/v1/README.md``).

- ``GET /ai-usage/ingest``: the versions, limits, server time, known tools, currencies and
  ``issue_ref_source`` values.
- ``POST /ai-usage/ingest``: a batch of usage records, upserted one by one.

The POST answers 200 with one result per record whenever the envelope is
readable, even when every record was rejected. Envelope problems are 400
(``invalid_json``, ``invalid_batch``, ``unsupported_schema``), a body over
1 MiB or 500 records is 413 (``batch_too_large``), and too many batches from
one account are 429 with ``Retry-After``. Both paths are served with and
without the trailing slash: reporters post to the contract's URL as written.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select

from specivo.core.ai_usage_ingest import (
    ISSUE_REF_METHODS,
    MAX_AGE_DAYS,
    MAX_BYTES,
    MAX_RECORDS,
    SUPPORTED_SCHEMAS,
    batch_validator,
    check_schema,
    too_deep,
    unknown_fields_warning,
)
from specivo.core.config import get_settings
from specivo.core.database import DbSession
from specivo.core.exceptions import AppError
from specivo.core.rate_limit import enforce_rate_limit
from specivo.core.security import get_current_user
from specivo.core.utils import utcnow
from specivo.models.currency import Currency
from specivo.models.user import User
from specivo.schemas.ai_usage import AiUsageIngestInfoOut, AiUsageIngestOut, AiUsageIngestRateLimitOut
from specivo.services.ai_subscription_service import AiToolService
from specivo.services.ai_usage_ingest_service import AiUsageIngestService

router = APIRouter(prefix="/ai-usage", tags=["ai-usage"])
_service = AiUsageIngestService()

_BATCH_BODY: dict[str, Any] = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "description": (
                    "A batch: {schema, client, records}. The full JSON Schema is "
                    "docs/contracts/ai-usage/v1/batch.schema.json."
                ),
                "required": ["schema", "client", "records"],
            }
        }
    },
}


def _too_large(message: str) -> AppError:
    return AppError(code="batch_too_large", message=message, status_code=413)


def _bad_batch(message: str, field: str | None = None, code: str = "invalid_batch") -> AppError:
    return AppError(code=code, message=message, status_code=400, field=field)


async def _rate_limit(request: Request, response: Response, user: User) -> None:
    limit = get_settings().ai_usage_ingest_rate_limit_per_minute
    await enforce_rate_limit(request, response, "ai_usage_ingest", limit, 60, identifier=f"user:{user.id}")


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BYTES:
                raise _too_large(f"The body is larger than {MAX_BYTES} bytes")
        except ValueError:
            raise _bad_batch("Invalid Content-Length") from None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BYTES:
            raise _too_large(f"The body is larger than {MAX_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/ingest", response_model=AiUsageIngestInfoOut, include_in_schema=False)
@router.get("/ingest/", response_model=AiUsageIngestInfoOut)
async def ingest_info(db: DbSession, current_user: User = Depends(get_current_user)) -> AiUsageIngestInfoOut:
    """What the ingest endpoint accepts: contract versions, limits, the server's clock, tools and currencies."""
    settings = get_settings()
    tools = await AiToolService().list_tools(db)
    currencies = (await db.execute(select(Currency.code).order_by(Currency.code))).scalars().all()
    return AiUsageIngestInfoOut(
        versions=list(SUPPORTED_SCHEMAS),
        max_records=MAX_RECORDS,
        max_bytes=MAX_BYTES,
        max_age_days=MAX_AGE_DAYS,
        rate_limit=(
            AiUsageIngestRateLimitOut(requests=settings.ai_usage_ingest_rate_limit_per_minute, window_seconds=60)
            if settings.rate_limit_enabled
            else None
        ),
        server_time=utcnow(),
        tools=[tool.slug for tool in tools],
        currencies=list(currencies),
        issue_ref_sources=list(ISSUE_REF_METHODS),
    )


@router.post("/ingest", response_model=AiUsageIngestOut, include_in_schema=False)
@router.post(
    "/ingest/",
    response_model=AiUsageIngestOut,
    openapi_extra={"requestBody": _BATCH_BODY},
    responses={
        400: {"description": "invalid_json, invalid_batch or unsupported_schema"},
        413: {"description": "batch_too_large: over 1 MiB or 500 records"},
        429: {"description": "rate_limit_exceeded, with Retry-After"},
    },
)
async def ingest(
    request: Request,
    response: Response,
    db: DbSession,
    current_user: User = Depends(get_current_user),
) -> AiUsageIngestOut:
    """Upsert a batch of AI usage records; one result per record (created, updated, unchanged or rejected).

    Each record is the full totals of one session x model x UTC-day bucket,
    keyed by ``(target user, idempotency_key)``: a replay replaces the totals.
    Authorization, attribution and costs are per record; see the contract.
    """
    await _rate_limit(request, response, current_user)
    raw = await _read_body(request)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _bad_batch("The body is not valid JSON", code="invalid_json") from None
    if too_deep(body):
        raise _bad_batch("The body is nested too deeply")
    if not isinstance(body, dict):
        raise _bad_batch("The body must be a JSON object")

    schema = body.get("schema")
    if not isinstance(schema, str):
        raise _bad_batch("schema is required", "schema")
    if schema not in SUPPORTED_SCHEMAS:
        raise AppError(
            code="unsupported_schema",
            message=f"Unsupported schema {schema[:100]!r}; this server accepts {', '.join(SUPPORTED_SCHEMAS)}",
            status_code=400,
            field="schema",
            details={"supported": list(SUPPORTED_SCHEMAS)},
        )
    sent = body.get("records")
    if isinstance(sent, list) and len(sent) > MAX_RECORDS:
        raise _too_large(f"A batch holds at most {MAX_RECORDS} records")

    # The envelope against the batch schema; the records are checked one by one.
    unknown, problem = check_schema(batch_validator(), body, skip=lambda path: len(path) >= 2 and path[0] == "records")
    if problem is not None:
        raise _bad_batch(problem.message.replace("the record", "the batch"), problem.field)
    batch_warnings = [unknown_fields_warning(unknown)] if unknown else []

    client = body["client"]
    records: list[Any] = body["records"]  # an array: the batch schema checked it
    results = await _service.ingest(
        db,
        current_user,
        records,
        client_name=client["name"].strip() or None,
        client_version=(client.get("version") or "").strip() or None,
        api_key_id=getattr(request.state, "api_key_id", None),
        request=request,
    )
    summary = {"received": len(results), "created": 0, "updated": 0, "unchanged": 0, "rejected": 0}
    for result in results:
        summary[result.status] += 1
    return AiUsageIngestOut.model_validate(
        {
            "schema": schema,
            "server_time": utcnow(),
            "summary": summary,
            "results": [result.as_dict() for result in results],
            "warnings": [w.as_dict() for w in batch_warnings],
        }
    )
