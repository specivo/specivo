"""Per-user budgets for AI usage charts and spend reports.

Every chart and report groups a whole user's or project's AI usage and
converts its amounts, so they are metered per caller wherever they are served:
the My AI usage page (its charts), the project and instance report pages, the
report API (JSON and CSV) and the MCP report tool share one ``ai_report``
bucket per user, so switching between them buys nothing. Administrators, who
also read the instance report, get ``AI_REPORT_RATE_LIMIT_ADMIN_PER_MINUTE``;
everyone else ``AI_REPORT_RATE_LIMIT_PER_MINUTE``. A caller without an account
(these pages need one, but the API dependency may still see one) is metered by
client address.

Over the limit a page answers with the rate-limit page and the API with its
JSON ``429`` (``specivo.core.exceptions``), both with ``Retry-After``. When
Redis is unavailable the request goes through, as with every other limit
(``specivo.core.rate_limit``).
"""

from __future__ import annotations

from fastapi import Request, Response

from specivo.core.client_ip import get_client_ip, rate_limit_subject
from specivo.core.config import get_settings
from specivo.core.rate_limit import RateLimiter, enforce_rate_limit
from specivo.models.user import User

AI_REPORT_BUCKET = "ai_report"
AI_REPORT_WINDOW_SECONDS = 60


def ai_report_limit(user: User | None) -> int:
    """Requests per minute *user* may make (the administrators' budget is higher)."""
    settings = get_settings()
    if user is not None and user.is_admin and not user.is_anonymous:
        return settings.ai_report_rate_limit_admin_per_minute
    return settings.ai_report_rate_limit_per_minute


def _identifier(user: User | None, request: Request | None) -> str:
    if user is not None and not user.is_anonymous:
        return f"user:{user.id}"
    ip = get_client_ip(request) if request is not None else None
    return "ip:" + rate_limit_subject(ip or "unknown")


async def enforce_ai_report_rate_limit(request: Request, response: Response, user: User | None) -> None:
    """Spend one chart or report from *user*'s budget; 429 when it is gone."""
    await enforce_rate_limit(
        request,
        response,
        AI_REPORT_BUCKET,
        ai_report_limit(user),
        AI_REPORT_WINDOW_SECONDS,
        identifier=_identifier(user, request),
    )


async def ai_report_allowed(user: User) -> tuple[bool, int]:
    """Spend one report from *user*'s budget outside a web request (the MCP tool): ``(allowed, retry_after)``."""
    if not get_settings().rate_limit_enabled:
        return True, 0
    limiter = RateLimiter(AI_REPORT_BUCKET, ai_report_limit(user), AI_REPORT_WINDOW_SECONDS)
    allowed, _remaining, retry_after = await limiter.check(_identifier(user, None))
    return allowed, retry_after
