"""Checks on ``APP_URL``, the public address every outside-facing URL is built from.

Repository webhook URLs, single sign-on redirect and back-channel logout URIs,
SCIM base URLs and the links in emails all start with ``APP_URL``. An install
that never set it keeps the default ``http://localhost:8030``, and the admin
pages then offer addresses that no forge or identity provider can reach.

:func:`app_url_problem` decides whether the value is such a problem;
:func:`app_url_notice` is what the shared warning box on those admin pages
shows; :func:`log_app_url_warning` is the one log line at start-up.

The request's ``Host`` header is only ever compared with ``APP_URL`` for the
"you are looking at this instance under another name" note. It is never used
to build a URL that is shown for copying or sent anywhere: it can be forged.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from starlette.requests import Request

    from specivo.core.config import Settings

logger = logging.getLogger(__name__)

# The guide section that documents APP_URL (docs/guide/en/install/configuration.md).
APP_URL_GUIDE_URL = "https://specivo.io/docs/specivo/install/configuration/#public-address-app_url"

AppUrlProblem = Literal["unset", "local"]


def _normalized_host(host: str | None) -> str:
    """``host`` lower-cased, without a trailing dot or IPv6 brackets."""
    return (host or "").strip().strip("[]").rstrip(".").lower()


def is_local_host(host: str | None) -> bool:
    """Whether ``host`` names this machine rather than an address others can reach.

    ``localhost`` and any ``*.localhost`` name, loopback addresses
    (``127.0.0.0/8``, ``::1``) and the unspecified ones (``0.0.0.0``, ``::``).
    """
    name = _normalized_host(host)
    if not name:
        return False
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def app_url_host(app_url: str) -> str:
    """The normalized host name of ``app_url`` ("" when it has none)."""
    try:
        return _normalized_host(urlsplit(app_url.strip()).hostname)
    except ValueError:
        return ""


def app_url_problem(app_url: str | None) -> AppUrlProblem | None:
    """Why ``app_url`` cannot be the public address of this instance, or ``None``.

    ``"unset"``: empty, or no host could be read from it. ``"local"``: the host
    is this machine (the default ``http://localhost:8030`` among them). A plain
    ``http://`` address on a real host is not reported here.
    """
    host = app_url_host(app_url or "")
    if not host:
        return "unset"
    if is_local_host(host):
        return "local"
    return None


def request_host_differs(app_url: str, request_host: str | None) -> bool:
    """Whether the page was opened under a host name other than ``APP_URL``'s.

    Only host names are compared: a reverse proxy usually passes the name
    without the port, so ports would only add false alarms. For display only.
    """
    expected = app_url_host(app_url)
    seen = _normalized_host(request_host)
    return bool(expected and seen and expected != seen)


def app_url_notice(request: Request | None) -> dict[str, object] | None:
    """What the APP_URL warning box shows on this request's page, or ``None`` for nothing.

    ``reason`` is ``"unset"`` or ``"local"`` (the value is wrong wherever it
    is opened), else ``"host_mismatch"`` when the page was opened under another
    host name than the one in ``APP_URL``.
    """
    from specivo.core.config import get_settings

    app_url = get_settings().app_url
    problem = app_url_problem(app_url)
    request_host = request.url.hostname if request is not None else None
    if problem is not None:
        reason: str = problem
    elif request_host_differs(app_url, request_host):
        reason = "host_mismatch"
    else:
        return None
    return {
        "reason": reason,
        "app_url": app_url,
        "request_host": _normalized_host(request_host),
        "guide_url": APP_URL_GUIDE_URL,
    }


def log_app_url_warning(settings: Settings, *, component: str = "app") -> bool:
    """Log one warning when a production instance runs with a local or unset ``APP_URL``.

    Silent with ``DEBUG`` on: a development instance on localhost is expected.
    Returns whether the warning was logged.
    """
    if settings.debug:
        return False
    problem = app_url_problem(settings.app_url)
    if problem is None:
        return False
    logger.warning(
        "APP_URL is %r (%s): webhook, single sign-on, SCIM and email links built by the %s "
        "point at this machine and cannot be reached from outside. Set APP_URL to the public "
        "address of Specivo, see %s",
        settings.app_url,
        "not set" if problem == "unset" else "a local address",
        component,
        APP_URL_GUIDE_URL,
    )
    return True


def public_url(path: str) -> str | None:
    """``APP_URL`` + ``STEALTH_PREFIX`` + *path*, the address of a page for someone outside.

    ``None`` when ``APP_URL`` has no host: a link without one cannot be
    followed from a mail client. A local ``APP_URL`` still gives a link (a
    development instance works with it); :func:`log_app_url_warning` already
    reported it at start-up.
    """
    from specivo.core.config import get_settings

    settings = get_settings()
    if app_url_problem(settings.app_url) == "unset":
        return None
    return f"{settings.app_url.strip().rstrip('/')}{settings.stealth_prefix.rstrip('/')}{path}"
