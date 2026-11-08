"""Per-IP search budgets, for everyone and in every mode.

A hybrid or semantic search runs the embedding model on the query, which
costs CPU on the server, and a keyword search over a large instance is not
free either. Search is therefore metered by client address on the search
page and on the REST search endpoint, whoever asks and in whichever mode:

- a visitor without an account spends the ``anon_search`` bucket
  (``SEARCH_RATE_LIMIT_ANONYMOUS_PER_MINUTE``);
- a signed-in caller spends the ``search`` bucket
  (``SEARCH_RATE_LIMIT_PER_MINUTE``), set higher because an office behind
  one NAT address shares it.

The bucket is keyed by the address :func:`specivo.core.client_ip.get_client_ip`
resolves (``X-Forwarded-For`` only from a trusted proxy), with an IPv6 client
counted by its /64 like every other per-IP limit. It is never keyed by user:
the cost being bounded is per machine sending queries, and the page and the
API share one budget so switching between them buys nothing.

When Redis is unavailable the limiter lets the request through, as every
other rate limit does (``specivo.core.rate_limit``).
"""

from __future__ import annotations

from fastapi import Request, Response

from specivo.core.client_ip import get_client_ip, rate_limit_subject
from specivo.core.config import get_settings
from specivo.core.rate_limit import enforce_rate_limit

ANONYMOUS_SEARCH_BUCKET = "anon_search"
SIGNED_IN_SEARCH_BUCKET = "search"
SEARCH_BUCKETS = frozenset({ANONYMOUS_SEARCH_BUCKET, SIGNED_IN_SEARCH_BUCKET})
SEARCH_WINDOW_SECONDS = 60


async def enforce_search_rate_limit(request: Request, response: Response, *, anonymous: bool) -> None:
    """Spend one search from this client address's budget; 429 when it is gone."""
    settings = get_settings()
    if anonymous:
        bucket, limit = ANONYMOUS_SEARCH_BUCKET, settings.search_rate_limit_anonymous_per_minute
    else:
        bucket, limit = SIGNED_IN_SEARCH_BUCKET, settings.search_rate_limit_per_minute
    subject = rate_limit_subject(get_client_ip(request) or "unknown")
    await enforce_rate_limit(request, response, bucket, limit, SEARCH_WINDOW_SECONDS, identifier=subject)
