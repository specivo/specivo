"""The ``specivo.sso`` logger, with a filter that redacts tokens, OAuth parameters and credentials.

All SSO modules log through :data:`logger`. They never pass tokens, codes,
verifiers or secrets to it; the filter is a second line of defence that
replaces, before a handler sees it:

* anything that looks like a compact JWS (``xxx.yyy.zzz`` in base64url
  starting with ``eyJ``, also when truncated);
* the value of a URL query parameter that carries a credential (``code``,
  ``state``, ``code_verifier``, ``id_token_hint``, ``logout_token``, ...);
* the value of an ``Authorization`` header.

The same filter sits on the ``httpx`` and ``httpcore`` loggers, whose request
lines carry full URLs (the callback URL holds ``code`` and ``state``).
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
# Only after ``?`` or ``&``: a space-separated log field such as ``code=sso_idp_error``
# is an error code, not an OAuth parameter.
_QUERY_SECRET_RE = re.compile(
    r"(?<=[?&])(code|state|code_verifier|client_secret|id_token_hint|id_token|logout_token"
    r"|access_token|refresh_token|nonce)=[^&\s\"'#]*",
    re.IGNORECASE,
)
_AUTHORIZATION_RE = re.compile(r"""(authorization['"]?\s*[:=]\s*['"]?)[^'"\r\n,}]+""", re.IGNORECASE)


def redact(text: str) -> str:
    """``text`` with JWT-shaped strings, credential query values and ``Authorization`` values replaced."""
    text = _JWT_RE.sub(REDACTED, text)
    text = _QUERY_SECRET_RE.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    return _AUTHORIZATION_RE.sub(lambda m: f"{m.group(1)}[redacted]", text)


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

for _name in ("specivo.sso", "httpx", "httpcore"):
    _target = logging.getLogger(_name)
    if not any(isinstance(f, RedactTokensFilter) for f in _target.filters):
        _target.addFilter(RedactTokensFilter())


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
    if settings.sso_force_local_login:
        logger.warning(
            "sso.startup.force_local_login SSO_FORCE_LOCAL_LOGIN is on: the password form is always shown, "
            "the login page never redirects to an identity provider, and every user may sign in with a "
            "password. Turn it off once single sign-on works again."
        )
