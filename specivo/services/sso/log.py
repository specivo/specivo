"""The ``specivo.sso`` logger, with a filter that redacts JWT-shaped strings.

All SSO modules log through :data:`logger`. They never pass tokens, codes,
verifiers or secrets to it; the filter is a second line of defence that
replaces anything that looks like a compact JWS (``xxx.yyy.zzz`` in base64url
starting with ``eyJ``, also when truncated) before a handler sees it.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from specivo.core.config import Settings

# A JWS, or the start of one (a truncated token still leaks its header and claims).
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]*){0,2}")
REDACTED = "[redacted-jwt]"


def redact(text: str) -> str:
    """``text`` with every JWT-shaped substring replaced."""
    return _JWT_RE.sub(REDACTED, text)


class RedactTokensFilter(logging.Filter):
    """Rewrites a record's message so no JWT-shaped string reaches a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        return True


logger = logging.getLogger("specivo.sso")
if not any(isinstance(f, RedactTokensFilter) for f in logger.filters):
    logger.addFilter(RedactTokensFilter())


def sanitize_idp_text(value: object, limit: int = 200) -> str:
    """IdP-supplied text made safe for a log line: printable ASCII, truncated, redacted."""
    text = value if isinstance(value, str) else repr(value)
    printable = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in redact(text))
    return redact(printable[:limit])


def log_startup_warnings(settings: Settings) -> None:
    """Log the SSO settings that weaken security, once, when the app starts."""
    if settings.sso_allow_insecure_http:
        logger.warning(
            "sso.startup.insecure_http SSO_ALLOW_INSECURE_HTTP is on: identity providers may use plain "
            "http:// URLs. Use this only with a disposable test identity provider, never in production."
        )
