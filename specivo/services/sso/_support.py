"""Helpers shared by the SSO protocol modules: HTTP error mapping and best-effort Redis.

Redis is a cache here, never a source of truth: every helper swallows Redis
errors (logging them) so a Redis outage degrades to "fetch from the IdP"
instead of failing the request. Callers that need Redis for correctness
(rate limits) check the return value.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError

from specivo.services.sso.errors import SsoIdpUnreachableError
from specivo.services.sso.http import (
    SSOBlockedAddressError,
    SSOInsecureURLError,
    SSORedirectError,
    SSOResponseTooLargeError,
)
from specivo.services.sso.log import logger

#: Errors a Redis call can raise when the server is unreachable or misbehaves.
REDIS_ERRORS: tuple[type[BaseException], ...] = (RedisError, OSError)


def unreachable_from(exc: httpx.HTTPError, *, what: str) -> SsoIdpUnreachableError:
    """Map an HTTP client error to ``sso_idp_unreachable`` with a precise reason."""
    if isinstance(exc, httpx.TimeoutException):
        reason = "timeout"
    elif isinstance(exc, SSORedirectError):
        reason = "redirect_refused"
    elif isinstance(exc, SSOInsecureURLError):
        reason = "insecure_url"
    elif isinstance(exc, SSOResponseTooLargeError):
        reason = "response_too_large"
    elif isinstance(exc, SSOBlockedAddressError):
        reason = "blocked_address"
    elif isinstance(exc, httpx.TooManyRedirects):
        reason = "too_many_redirects"
    else:
        reason = "connection_failed"
    logger.warning("sso.%s.unreachable reason=%s error=%s", what, reason, type(exc).__name__)
    return SsoIdpUnreachableError(reason)


def parse_json_object(content: bytes) -> dict[str, Any] | None:
    """The body as a JSON object, or ``None`` when it is not one."""
    try:
        value = json.loads(content)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


async def cache_get(redis: Redis, key: str) -> str | None:
    try:
        value = await redis.get(key)
    except REDIS_ERRORS as exc:
        logger.warning("sso.redis.get_failed key=%s error=%s", key, type(exc).__name__)
        return None
    return value if isinstance(value, str) else None


async def cache_set(redis: Redis, key: str, value: str, ttl: int) -> None:
    try:
        await redis.set(key, value, ex=ttl)
    except REDIS_ERRORS as exc:
        logger.warning("sso.redis.set_failed key=%s error=%s", key, type(exc).__name__)


async def cache_delete(redis: Redis, *keys: str) -> None:
    try:
        await redis.delete(*keys)
    except REDIS_ERRORS as exc:
        logger.warning("sso.redis.delete_failed error=%s", type(exc).__name__)


async def acquire_once(redis: Redis, key: str, ttl: int) -> bool | None:
    """``SET key NX EX ttl``: True if acquired, False if held, None if Redis failed."""
    try:
        return bool(await redis.set(key, "1", ex=ttl, nx=True))
    except REDIS_ERRORS as exc:
        logger.warning("sso.redis.lock_failed key=%s error=%s", key, type(exc).__name__)
        return None
