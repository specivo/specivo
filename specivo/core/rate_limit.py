"""Redis-backed sliding window rate limiter.

Uses Redis sorted sets (ZSET) to implement a sliding window algorithm:
- Each request adds an entry scored by the current timestamp (microseconds).
- Entries older than the window are removed before each check.
- The count of remaining entries determines whether the request is allowed.

Graceful degradation: if Redis is unavailable the limiter allows all requests
and logs a warning. Never deny service because the rate limiter is broken.

Usage
-----
Apply to an endpoint as a FastAPI dependency::

    from specivo.core.rate_limit import rate_limit

    @router.post("/login")
    async def login(
        _rl: Annotated[None, Depends(rate_limit("auth_login", max_requests=10, window_seconds=60))],
        ...
    ):
        ...
"""

from __future__ import annotations

import logging
import time

from fastapi import Request, Response

from specivo.core.exceptions import AppError

logger = logging.getLogger(__name__)

# Header names (standard de-facto)
_HEADER_LIMIT = "X-RateLimit-Limit"
_HEADER_REMAINING = "X-RateLimit-Remaining"
_HEADER_RESET = "X-RateLimit-Reset"
_HEADER_RETRY_AFTER = "Retry-After"


class RateLimiter:
    """Sliding window rate limiter backed by a Redis sorted set.

    Key format: ``rl:{key_prefix}:{identifier}``

    The sorted set uses microsecond timestamps as scores so multiple
    requests in the same millisecond do not collide on member names.
    TTL is set to ``window_seconds`` on every write — this caps memory
    consumption even for keys that stop receiving traffic.
    """

    def __init__(self, key_prefix: str, max_requests: int, window_seconds: int) -> None:
        self.key_prefix = key_prefix
        self.max_requests = max_requests
        self.window_seconds = window_seconds

    async def check(self, identifier: str) -> tuple[bool, int, int]:
        """Check whether a request from *identifier* is within the rate limit.

        Returns a 3-tuple:
        - ``allowed`` — True if the request should proceed.
        - ``remaining`` — number of requests still allowed in this window.
        - ``retry_after`` — seconds until the next request is allowed (0 if
          allowed, positive integer if rate limit was exceeded).
        """
        try:
            from specivo.core.redis import get_redis

            redis = await get_redis()
        except Exception as exc:
            logger.warning("Rate limiter: Redis unavailable (%s) — allowing request", exc)
            return True, self.max_requests - 1, 0

        key = f"rl:{self.key_prefix}:{identifier}"
        now_us = int(time.time() * 1_000_000)  # microseconds for uniqueness
        window_start_us = now_us - (self.window_seconds * 1_000_000)

        try:
            pipe = redis.pipeline()
            # Remove entries older than the sliding window
            pipe.zremrangebyscore(key, "-inf", window_start_us)
            # Count current entries (after removal)
            pipe.zcard(key)
            # Add this request with current timestamp as score and member
            pipe.zadd(key, {str(now_us): now_us})
            # Reset TTL so idle keys expire from Redis automatically
            pipe.expire(key, self.window_seconds + 1)
            results = await pipe.execute()
        except Exception as exc:
            logger.warning("Rate limiter: Redis pipeline error (%s) — allowing request", exc)
            return True, self.max_requests - 1, 0

        count_before = results[1]  # count BEFORE adding the current request

        if count_before >= self.max_requests:
            # Over limit — find the oldest entry to compute retry_after
            try:
                oldest = await redis.zrange(key, 0, 0, withscores=True)
                if oldest:
                    oldest_score_us = int(oldest[0][1])
                    retry_after = max(
                        1,
                        self.window_seconds - int((now_us - oldest_score_us) / 1_000_000),
                    )
                else:
                    retry_after = self.window_seconds
            except Exception:
                retry_after = self.window_seconds
            # Remove the entry we just added since we're rejecting the request
            try:
                await redis.zrem(key, str(now_us))
            except Exception:
                pass
            return False, 0, retry_after

        remaining = self.max_requests - count_before - 1
        return True, max(0, remaining), 0


def _get_client_ip(request: Request) -> str:
    """Extract the client IP address from the request.

    Only trusts ``X-Forwarded-For`` when the direct connection peer
    (``request.client.host``) is listed in the XFF chain AND matches a
    CIDR in ``settings.trusted_proxies``. When trusted, the leftmost
    (original client) IP from XFF is returned.

    When no trusted proxy is configured or the peer is not trusted,
    ``request.client.host`` is returned directly.

    Returns ``"unknown"`` if no client information is available.
    """
    import ipaddress

    from specivo.core.config import get_settings

    client_host = request.client.host if request.client else None

    if client_host:
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            xff_parts = [p.strip() for p in forwarded_for.split(",")]

            # Check if the direct peer appears in the XFF chain — a sign
            # that it is a proxy that appended itself.
            if client_host in xff_parts:
                settings = get_settings()

                # Determine if the direct peer is a trusted proxy
                is_trusted = False
                if settings.trusted_proxies:
                    try:
                        client_addr = ipaddress.ip_address(client_host)
                        is_trusted = any(
                            client_addr in ipaddress.ip_network(cidr, strict=False) for cidr in settings.trusted_proxies
                        )
                    except (ValueError, TypeError):
                        pass

                # SECURITY: Only trust IPs explicitly listed in trusted_proxies.
                # Do NOT fall back to is_private — an attacker on the local network
                # could spoof X-Forwarded-For to bypass per-IP rate limits.
                if is_trusted:
                    return xff_parts[0]

        return client_host

    return "unknown"


def rate_limit(key_prefix: str, max_requests: int, window_seconds: int):
    """FastAPI dependency factory for rate limiting.

    Extracts the rate limit identifier:
    - Authenticated requests (``request.state.rate_limit_user_id`` set by
      a prior dependency): uses the user/API-key ID.
    - All other requests: uses the client IP address.

    On limit exceeded: raises ``AppError(429)`` with ``Retry-After`` header.
    On allowed:        adds ``X-RateLimit-*`` headers to the response.

    Example::

        @router.post("/login")
        async def login(
            _rl: Annotated[None, Depends(rate_limit("auth_login", 10, 60))],
            ...
        ):
            ...
    """
    limiter = RateLimiter(key_prefix, max_requests, window_seconds)

    async def _dependency(request: Request, response: Response) -> None:
        # Prefer an authenticated user ID if one has been resolved upstream.
        # For auth endpoints (login) we use IP because the user is not yet known.
        user_id: str | None = getattr(request.state, "rate_limit_user_id", None)
        identifier = str(user_id) if user_id else _get_client_ip(request)

        allowed, remaining, retry_after = await limiter.check(identifier)

        # Build rate limit headers
        rl_headers = {
            _HEADER_LIMIT: str(max_requests),
            _HEADER_REMAINING: str(remaining),
            _HEADER_RESET: str(int(time.time()) + window_seconds),
        }

        # Always attach informational headers on allowed requests.
        # Set them both on the injected Response (works when the endpoint
        # returns a model) and on request.state (so middleware can copy
        # them onto custom Response objects like JSONResponse).
        for k, v in rl_headers.items():
            response.headers[k] = v
        request.state.rate_limit_headers = dict(rl_headers)

        if not allowed:
            rl_headers[_HEADER_RETRY_AFTER] = str(retry_after)
            raise AppError(
                code="rate_limit_exceeded",
                message=f"Rate limit exceeded. Try again in {retry_after} second(s).",
                status_code=429,
                details={"retry_after": retry_after},
                headers=rl_headers,
            )

    return _dependency
