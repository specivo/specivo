"""SCIM 2.0 endpoint: ``/scim/v2/{provider_slug}/...`` (RFC 7644), one per SSO provider.

Called server to server by the identity provider, never by a browser: no
cookies, no CSRF token (requests carry ``Authorization``, which the CSRF
middleware already exempts), JSON in and out as ``application/scim+json``
(``application/json`` is accepted too). In order, for every request:

1. A body over ``SCIM_MAX_BODY_BYTES`` is refused with 413, by its
   ``Content-Length`` and again while reading (and, once authenticated, a
   body nested deeper than 16 levels with 400).
2. The bearer token is checked against the provider's SCIM token
   (:meth:`~specivo.services.scim.config_service.ScimConfigService.authenticate`).
   An unknown slug, a provider with SCIM off and a missing, wrong or revoked
   token all get the same 401; failures spend the client address's bucket
   (``SCIM_UNAUTHENTICATED_RATE_LIMIT_PER_MINUTE``), not the provider's.
3. Rate limit per provider (``SCIM_RATE_LIMIT_PER_MINUTE``): 429.

Paths follow the SCIM protocol (``/Users``, ``/Users/{id}``) rather than the
trailing-slash rule of the JSON API; both spellings are routed, because SCIM
clients do not follow redirects. Errors are SCIM error bodies
(:mod:`specivo.services.scim.errors`), and an error rolls the request's
database work back.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.client_ip import get_client_ip, rate_limit_subject
from specivo.core.config import get_settings
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import AppError
from specivo.core.rate_limit import enforce_rate_limit
from specivo.models.sso import SsoProvider
from specivo.services.scim import resources
from specivo.services.scim.config_service import ScimConfigService, scim_base_url
from specivo.services.scim.errors import ScimError, error_body
from specivo.services.scim.filters import Comparison, parse_filter
from specivo.services.scim.groups import ScimGroupService
from specivo.services.scim.users import ScimUserService

__all__ = ["SCIM_MEDIA_TYPE", "scim_error_handler", "scim_router"]

SCIM_MEDIA_TYPE = "application/scim+json"
# SCIM bodies nest a few levels (PatchOp > Operations > value > emails > entry);
# deeper ones are refused before the PATCH engine copies them recursively.
_MAX_JSON_DEPTH = 16
# A startIndex past this is past every result (and still fits a database offset).
_MAX_START_INDEX = 2**31
_ACCEPTED_MEDIA_TYPES = frozenset({SCIM_MEDIA_TYPE, "application/json"})

scim_router = APIRouter(prefix="/scim/v2/{slug}", tags=["scim"], include_in_schema=False)
_config = ScimConfigService()
_users = ScimUserService()
_groups = ScimGroupService()


class ScimResponse(JSONResponse):
    media_type = SCIM_MEDIA_TYPE


async def scim_error_handler(request: Request, exc: Exception) -> Response:
    """Render a :class:`ScimError` as a SCIM error body."""
    assert isinstance(exc, ScimError)
    return ScimResponse(error_body(exc), status_code=exc.status, headers=exc.headers)


def _route(method: str, path: str, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``path`` with and without a trailing slash."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        scim_router.add_api_route(path, func, methods=[method], **kwargs)
        scim_router.add_api_route(path + "/", func, methods=[method], **kwargs)
        return func

    return decorator


# ---------------------------------------------------------------------------
# Authentication, limits, body
# ---------------------------------------------------------------------------


def _too_large() -> ScimError:
    return ScimError(413, "The request body is too large.")


async def _rate_limit(request: Request, response: Response, key: str, limit: int, identifier: str) -> None:
    try:
        await enforce_rate_limit(request, response, key, limit, 60, identifier=identifier)
    except AppError as exc:
        if exc.status_code != 429:
            raise
        raise ScimError(429, "Too many requests.", headers=exc.headers) from None


async def authenticated_provider(
    slug: str, request: Request, response: Response, db: AsyncSession = DB_SESSION
) -> SsoProvider:
    """The provider whose SCIM token the request carries, after the size and rate checks."""
    settings = get_settings()
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > settings.scim_max_body_bytes:
                raise _too_large()
        except ValueError:
            raise ScimError.invalid_syntax("Invalid Content-Length.") from None

    provider = await _config.authenticate(db, slug, request.headers.get("authorization"))
    if provider is None:
        subject = rate_limit_subject(get_client_ip(request) or "unknown")
        await _rate_limit(
            request, response, "scim_unauth", settings.scim_unauthenticated_rate_limit_per_minute, subject
        )
        raise ScimError.unauthorized()
    await _rate_limit(request, response, "scim", settings.scim_rate_limit_per_minute, str(provider.id))
    await _config.touch(db, provider)
    return provider


Provider = Annotated[SsoProvider, Depends(authenticated_provider)]


async def _json_body(request: Request) -> Any:
    content_type = request.headers.get("content-type")
    if content_type is not None:
        media = content_type.split(";", 1)[0].strip().lower()
        if media not in _ACCEPTED_MEDIA_TYPES:
            raise ScimError(415, f"Send the body as {SCIM_MEDIA_TYPE}.")
    limit = get_settings().scim_max_body_bytes
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _too_large()
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ScimError.invalid_syntax("The request body is not valid JSON.") from None
    if _too_deep(body):
        raise ScimError.invalid_syntax(f"The request body is nested deeper than {_MAX_JSON_DEPTH} levels.")
    return body


def _too_deep(value: Any) -> bool:
    """Whether ``value`` nests lists and objects deeper than ``_MAX_JSON_DEPTH`` (checked without recursion)."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children: Any = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        if depth > _MAX_JSON_DEPTH:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


# ---------------------------------------------------------------------------
# Query parameters
# ---------------------------------------------------------------------------


def _int_param(request: Request, name: str, default: int | None) -> int | None:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ScimError.invalid_value(f"{name} must be an integer.") from None


def _paging(request: Request) -> tuple[int, int]:
    """``startIndex`` (1-based, at least 1) and ``count`` (0 to ``SCIM_MAX_RESULTS``)."""
    max_results = get_settings().scim_max_results
    start = min(max(1, _int_param(request, "startIndex", 1) or 1), _MAX_START_INDEX)
    count = _int_param(request, "count", max_results)
    return start, min(max(0, count if count is not None else max_results), max_results)


def _filter(request: Request, resource_type: str) -> list[Comparison]:
    text = request.query_params.get("filter")
    if text is None:
        return []
    return parse_filter(text, "User" if resource_type == "User" else "Group")


def _names(request: Request, param: str) -> set[str]:
    raw = request.query_params.get(param) or ""
    names = set()
    for part in raw.split(","):
        part = part.strip().lower()
        if part:
            names.add(part.rsplit(":", 1)[-1] if part.startswith("urn:") else part)
    return names


_ALWAYS_RETURNED = frozenset({"schemas", "id", "meta"})


def _project(resource: dict[str, Any], request: Request) -> dict[str, Any]:
    """Apply ``attributes`` / ``excludedAttributes`` (top-level attributes)."""
    wanted = _names(request, "attributes")
    excluded = _names(request, "excludedattributes") | _names(request, "excludedAttributes")
    if wanted:
        return {k: v for k, v in resource.items() if k.lower() in wanted or k in _ALWAYS_RETURNED}
    if excluded:
        return {k: v for k, v in resource.items() if k.lower() not in excluded or k in _ALWAYS_RETURNED}
    return resource


def _members_wanted(request: Request) -> bool:
    wanted = _names(request, "attributes")
    if wanted:
        return "members" in wanted
    return "members" not in (_names(request, "excludedattributes") | _names(request, "excludedAttributes"))


def _created(resource: dict[str, Any]) -> ScimResponse:
    return ScimResponse(resource, status_code=201, headers={"Location": resource["meta"]["location"]})


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@_route("GET", "/ServiceProviderConfig")
async def service_provider_config(slug: str, provider: Provider) -> ScimResponse:
    """What this SCIM service supports (static)."""
    return ScimResponse(
        resources.service_provider_config(scim_base_url(provider.slug), get_settings().scim_max_results)
    )


@_route("GET", "/ResourceTypes")
async def resource_types(slug: str, provider: Provider) -> ScimResponse:
    types = resources.resource_types(scim_base_url(provider.slug))
    return ScimResponse(resources.list_response(types, total=len(types), start_index=1))


@_route("GET", "/ResourceTypes/{type_id}")
async def resource_type(slug: str, type_id: str, provider: Provider) -> ScimResponse:
    for item in resources.resource_types(scim_base_url(provider.slug)):
        if item["id"] == type_id:
            return ScimResponse(item)
    raise ScimError.not_found("Resource type not found")


@_route("GET", "/Schemas")
async def schemas(slug: str, provider: Provider) -> ScimResponse:
    items = resources.schemas(scim_base_url(provider.slug))
    return ScimResponse(resources.list_response(items, total=len(items), start_index=1))


@_route("GET", "/Schemas/{schema_id}")
async def schema(slug: str, schema_id: str, provider: Provider) -> ScimResponse:
    for item in resources.schemas(scim_base_url(provider.slug)):
        if item["id"] == schema_id:
            return ScimResponse(item)
    raise ScimError.not_found("Schema not found")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@_route("GET", "/Users")
async def list_users(slug: str, request: Request, provider: Provider, db: AsyncSession = DB_SESSION) -> ScimResponse:
    """List the provider's users; ``filter``, ``startIndex`` and ``count`` as in RFC 7644 section 3.4.2."""
    comparisons = _filter(request, "User")
    start, count = _paging(request)
    total, rows = await _users.search(db, provider, comparisons=comparisons, start_index=start, count=count)
    items = [_project(_users.to_resource(provider, res, user), request) for res, user in rows]
    return ScimResponse(resources.list_response(items, total=total, start_index=start))


@_route("POST", "/Users")
async def create_user(slug: str, request: Request, provider: Provider, db: AsyncSession = DB_SESSION) -> ScimResponse:
    res, user = await _users.create(db, provider, await _json_body(request))
    return _created(_users.to_resource(provider, res, user))


@_route("GET", "/Users/{scim_id}")
async def get_user(
    slug: str, scim_id: str, request: Request, provider: Provider, db: AsyncSession = DB_SESSION
) -> ScimResponse:
    res, user = await _users.get(db, provider, scim_id)
    return ScimResponse(_project(_users.to_resource(provider, res, user), request))


@_route("PUT", "/Users/{scim_id}")
async def replace_user(
    slug: str, scim_id: str, request: Request, provider: Provider, db: AsyncSession = DB_SESSION
) -> ScimResponse:
    res, user = await _users.replace(db, provider, scim_id, await _json_body(request))
    return ScimResponse(_users.to_resource(provider, res, user))


@_route("PATCH", "/Users/{scim_id}")
async def patch_user(
    slug: str, scim_id: str, request: Request, provider: Provider, db: AsyncSession = DB_SESSION
) -> ScimResponse:
    res, user = await _users.patch(db, provider, scim_id, await _json_body(request))
    return ScimResponse(_users.to_resource(provider, res, user))


@_route("DELETE", "/Users/{scim_id}", status_code=204)
async def delete_user(slug: str, scim_id: str, provider: Provider, db: AsyncSession = DB_SESSION) -> Response:
    await _users.delete(db, provider, scim_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


async def _group_resource(
    db: AsyncSession, provider: SsoProvider, res: Any, group: Any, request: Request | None
) -> dict[str, Any]:
    with_members = request is None or _members_wanted(request)
    members = (await _groups.members(db, provider, [group.id]))[group.id] if with_members else None
    body = _groups.to_resource(provider, res, group, members)
    return _project(body, request) if request is not None else body


@_route("GET", "/Groups")
async def list_groups(slug: str, request: Request, provider: Provider, db: AsyncSession = DB_SESSION) -> ScimResponse:
    comparisons = _filter(request, "Group")
    start, count = _paging(request)
    total, rows = await _groups.search(db, provider, comparisons=comparisons, start_index=start, count=count)
    members = await _groups.members(db, provider, [g.id for _r, g in rows]) if _members_wanted(request) else None
    items = [
        _project(_groups.to_resource(provider, res, group, members[group.id] if members is not None else None), request)
        for res, group in rows
    ]
    return ScimResponse(resources.list_response(items, total=total, start_index=start))


@_route("POST", "/Groups")
async def create_group(slug: str, request: Request, provider: Provider, db: AsyncSession = DB_SESSION) -> ScimResponse:
    res, group = await _groups.create(db, provider, await _json_body(request))
    return _created(await _group_resource(db, provider, res, group, None))


@_route("GET", "/Groups/{scim_id}")
async def get_group(
    slug: str, scim_id: str, request: Request, provider: Provider, db: AsyncSession = DB_SESSION
) -> ScimResponse:
    res, group = await _groups.get(db, provider, scim_id)
    return ScimResponse(await _group_resource(db, provider, res, group, request))


@_route("PUT", "/Groups/{scim_id}")
async def replace_group(
    slug: str, scim_id: str, request: Request, provider: Provider, db: AsyncSession = DB_SESSION
) -> ScimResponse:
    res, group = await _groups.replace(db, provider, scim_id, await _json_body(request))
    return ScimResponse(await _group_resource(db, provider, res, group, None))


@_route("PATCH", "/Groups/{scim_id}", status_code=204)
async def patch_group(
    slug: str, scim_id: str, request: Request, provider: Provider, db: AsyncSession = DB_SESSION
) -> Response:
    """Apply a PATCH; answered with 204 (RFC 7644 section 3.5.2), so a large group is not sent back each time."""
    await _groups.patch(db, provider, scim_id, await _json_body(request))
    return Response(status_code=204)


@_route("DELETE", "/Groups/{scim_id}", status_code=204)
async def delete_group(slug: str, scim_id: str, provider: Provider, db: AsyncSession = DB_SESSION) -> Response:
    await _groups.delete(db, provider, scim_id)
    return Response(status_code=204)
