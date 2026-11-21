"""Checks on ``APP_URL``, the public address every outside-facing URL is built from.

Repository webhook URLs, single sign-on redirect and back-channel logout URIs,
SCIM base URLs and the links in emails all start with ``APP_URL``. An install
that never set it keeps the default ``http://localhost:8030``, and the admin
pages then offer addresses that no forge or identity provider can reach.

:func:`app_url_problem` decides whether the value is such a problem;
:func:`app_url_notice` is what the shared warning box on those admin pages
shows; :func:`log_app_url_warning` is the one log line at start-up.

:func:`base_path` and :func:`prefixed` are the one place the ``STEALTH_PREFIX``
is read: every route is mounted under it, and every root-relative URL the
application hands out (page links, redirects, static assets, API calls made
by the pages) carries it. Without a prefix they change nothing.

The request's ``Host`` header is only ever compared with ``APP_URL`` for the
"you are looking at this instance under another name" note. It is never used
to build a URL that is shown for copying or sent anywhere: it can be forged.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from starlette.requests import Request

    from specivo.core.config import Settings

logger = logging.getLogger(__name__)

# The guide section that documents APP_URL (docs/guide/en/install/configuration.md).
APP_URL_GUIDE_URL = "https://specivo.io/docs/specivo/install/configuration/#public-address-app_url"

AppUrlProblem = Literal["unset", "local"]

# One path segment of STEALTH_PREFIX: the unreserved URL characters only, so
# the prefix needs no escaping wherever it is written (HTML attributes, JS
# string literals in templates, Location headers, cookie paths).
_PREFIX_SEGMENT_RE = re.compile(r"[A-Za-z0-9._~-]+")


def normalize_prefix(raw: str | None) -> str:
    """``STEALTH_PREFIX`` as ``""`` or ``"/one/or/more"``: one leading slash, none trailing.

    Raises ``ValueError`` for a segment with characters other than letters,
    digits and ``.``, ``_``, ``~``, ``-``, or a ``.``/``..`` segment.
    """
    value = (raw or "").strip().strip("/")
    if not value:
        return ""
    segments = value.split("/")
    for segment in segments:
        if not _PREFIX_SEGMENT_RE.fullmatch(segment) or segment in {".", ".."}:
            raise ValueError(
                f"STEALTH_PREFIX: {raw!r} must be path segments of letters, digits, '.', '_', '~' and '-' only"
            )
    return "/" + "/".join(segments)


def base_path(settings: Settings | None = None) -> str:
    """The URL prefix every route of this instance lives under: ``""`` or ``"/prefix"``.

    *settings* defaults to the application's.
    """
    if settings is None:
        from specivo.core.config import get_settings

        settings = get_settings()
    return normalize_prefix(settings.stealth_prefix)


def prefixed(path: str, settings: Settings | None = None) -> str:
    """*path*, an application path such as ``/projects/``, as the browser must request it.

    Not idempotent on purpose: pass a path that does not carry the prefix
    yet. Paths taken from the request (``request.url.path``, a ``next``
    parameter) already carry it and must not go through here again.
    """
    if not path.startswith("/"):
        raise ValueError(f"prefixed() takes a root-relative path, got {path!r}")
    return base_path(settings) + path


def is_under_base(path: str) -> bool:
    """Whether the root-relative *path* lies under the URL prefix (always true without one)."""
    base = base_path()
    if not base:
        return path.startswith("/")
    return path == base or path.startswith((base + "/", base + "?", base + "#"))


def strip_base(path: str) -> str:
    """*path* without the URL prefix, the application path a route was declared with.

    A path outside the prefix comes back unchanged.
    """
    base = base_path()
    if base and is_under_base(path):
        rest = path[len(base) :]
        return rest if rest.startswith("/") else "/" + rest
    return path


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
    return absolute_url(path, settings)


def absolute_url(path: str, settings: Settings | None = None) -> str:
    """``APP_URL`` + ``STEALTH_PREFIX`` + *path*, whatever ``APP_URL`` holds.

    For URLs another system is configured with (identity provider redirect
    URIs, SCIM base URLs, repository webhook URLs), which are shown even when
    ``APP_URL`` still needs fixing. *settings* defaults to the application's.
    """
    if settings is None:
        from specivo.core.config import get_settings

        settings = get_settings()
    return f"{settings.app_url.strip().rstrip('/')}{base_path(settings)}{path}"
