"""Reading euro reference rates from the ECB, or from a feed that serves the same data.

The optional daily fetch (off by default, see
:class:`specivo.services.exchange_rate_service.ExchangeRateService`) downloads
one URL and stores what it holds as ``source = "ecb"`` rates. Three formats are
understood, detected from the body:

- the ECB euro reference rates XML (``eurofxref-daily.xml``, also the
  ``-hist-90d`` and ``-hist`` files): ``<Cube time="..."><Cube currency="USD"
  rate="1.1224"/>...``, every rate against EUR;
- Frankfurter v1 JSON (``/v1/latest``, ``/v1/2026-11-05``, time series):
  ``{"amount": 1, "base": "EUR", "date": "...", "rates": {"USD": 1.1224}}``;
- Frankfurter v2 JSON (``/v2/rates``): ``[{"date": "...", "base": "EUR",
  "quote": "USD", "rate": 1.1224}, ...]``.

**Parsing.** The body must be UTF-8 (an optional UTF-8 BOM is allowed); any
other encoding, a NUL or other control character, an XML declaration naming
another encoding, a DOCTYPE or an entity declaration is refused before the XML
parser sees the text. Numbers are read as ``Decimal`` straight from the text
(``json`` with ``parse_float=Decimal``), never through a float.

**Errors.** :class:`FeedError` messages are stored and shown on the admin
page, so they name a category and a position ("invalid rate in item 3"),
never content taken from the response. Details go to the debug log, cut
short.

**Network.** Every request, redirects included (at most 3), goes through
:class:`_FeedGuardTransport`, which refuses link-local and cloud metadata
addresses always, and loopback, private, unique-local, multicast and reserved
addresses unless the operator sets ``EXCHANGE_RATE_FEED_ALLOW_PRIVATE_NETWORKS``
(for a self-hosted Frankfurter on the internal network). Host names are
resolved and every answer checked; without a proxy the connection is pinned
to the checked addresses, so DNS rebinding cannot swap them. Numeric host
spellings other than a canonical dotted quad (``0x7f.1``, ``2130706433``) are
refused. The whole fetch, body included, has one deadline
(:data:`FEED_TIMEOUT_SECONDS`), and the body is capped at
:data:`MAX_FEED_BYTES`.

**Proxies.** ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``NO_PROXY`` from the
environment are honoured, like the model download, but applied here, per
request and redirect hop, rather than by httpx (``trust_env=False``), so the
guard still sees the target host. Through a proxy the target is resolved and
checked here first; a host that does not resolve here is refused unless the
operator sets ``EXCHANGE_RATE_FEED_ALLOW_UNRESOLVED_VIA_PROXY``. The proxy then
resolves the name again, so this check cannot stop DNS rebinding through it:
**a configured proxy must enforce egress rules to private and link-local
ranges itself.** Failures (no network on an air-gapped host, a proxy refusing,
a bad body) raise :class:`FeedError`; nothing is retried until the next
scheduled run.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

#: The ECB's daily euro reference rates (about 30 currencies, updated around 16:00 CET on working days).
DEFAULT_FEED_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

#: Largest body accepted. The full ECB history since 1999 is about 6 MB.
MAX_FEED_BYTES = 8 * 1024 * 1024

#: Most rates one fetch may store.
MAX_FEED_RATES = 250_000

#: Deadline for the whole fetch: connecting, every redirect hop and the body.
FEED_TIMEOUT_SECONDS = 20.0
MAX_REDIRECTS = 3

_CODE_RE = re.compile(r"^[A-Z]{3}$")
_FORBIDDEN_XML = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_XML_DECL_ENCODING = re.compile(r"^\s*<\?xml[^>]*\bencoding\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_NUMERIC_LABEL = re.compile(r"^(0[xX][0-9a-fA-F]*|[0-9]+)$")
_OTHER_BOMS = (b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")

Resolver = Callable[[str, int], Awaitable[list[str]]]


class FeedError(Exception):
    """The feed could not be fetched or read. The message is stored and shown to administrators."""


class FeedBlockedAddressError(httpx.RequestError):
    """A request (or a redirect) would reach an address the fetch never contacts."""


class FeedUnresolvedError(httpx.RequestError):
    """Through a proxy, the target host does not resolve here, so it cannot be checked."""


@dataclass(frozen=True, slots=True)
class FeedRate:
    """``1 base = rate quote`` on ``rate_date``, as the feed published it."""

    rate_date: date
    base: str
    quote: str
    rate: Decimal


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------


def is_noncanonical_numeric_host(host: str) -> bool:
    """A host made of numeric labels that is not a canonical address (``0x7f.1``, ``2130706433``, ``017.0.0.1``).

    Resolvers read such names as IP addresses in surprising ways, so they are
    refused rather than checked.
    """
    labels = host.rstrip(".").split(".")
    if not labels or not all(_NUMERIC_LABEL.match(label) for label in labels):
        return False
    try:
        return str(ipaddress.IPv4Address(host)) != host
    except ValueError:
        return True


def is_forbidden_address(address: str, *, allow_private: bool) -> bool:
    """Whether the fetch must not contact *address* (an IP address string).

    Link-local and cloud metadata addresses always; loopback, private,
    unique-local, unspecified, multicast and reserved ones unless
    *allow_private*. IPv4-mapped and NAT64 forms count as their IPv4 address.
    """
    from specivo.services.sso.http import is_blocked_address

    if is_blocked_address(address):
        return True
    if allow_private:
        return False
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        candidates.append(ip.ipv4_mapped)
    return any(
        c.is_private or c.is_loopback or c.is_link_local or c.is_unspecified or c.is_multicast or c.is_reserved
        for c in candidates
    )


def validate_feed_url(url: str) -> str:
    """Return *url* stripped, or raise ``ValueError`` when it is not a usable http(s) URL.

    Checks the form only: scheme, host, no credentials, no non-canonical
    numeric host, no link-local or metadata IP literal. Where a host name
    leads is checked when the feed is fetched.
    """
    from specivo.services.sso.http import is_blocked_address

    text = (url or "").strip()
    if len(text) > 500:
        raise ValueError("The URL is too long (500 characters at most)")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Enter an http:// or https:// URL")
    if parts.username or parts.password:
        raise ValueError("The URL must not contain a user name or password")
    if is_noncanonical_numeric_host(parts.hostname):
        raise ValueError("That address cannot be used")
    try:
        blocked = is_blocked_address(parts.hostname)
    except ValueError:
        blocked = False  # a host name; checked when it is resolved
    if blocked:
        raise ValueError("That address cannot be used")
    return text


async def _resolve_host(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


ProxyFor = Callable[[str], "str | None"]


class _FeedGuardTransport(httpx.AsyncBaseTransport):
    """Checks the destination of every request; each redirect hop is a request of its own.

    For every request the guard picks the route again (*proxy_for*: direct, or
    which proxy), then:

    - refuses non-canonical numeric hosts and forbidden IP literals;
    - with a *resolver*, resolves the host and refuses it when any answer is
      forbidden (:func:`is_forbidden_address`), or when it does not resolve
      (fail closed, also through a proxy, unless *allow_unresolved_via_proxy*);
    - direct: connects to the checked addresses only (pinned, with the original
      ``Host`` header and TLS server name), so DNS cannot change between the
      check and the connection;
    - through a proxy: hands the original request to the proxy. The proxy
      resolves the name again, so a host that answers differently the second
      time (DNS rebinding) is not caught here: **when a proxy is configured it
      must itself refuse private and link-local destinations.**

    *resolver* ``None`` checks IP literals only (the test seam with a mock
    transport).
    """

    def __init__(
        self,
        direct: httpx.AsyncBaseTransport,
        *,
        resolver: Resolver | None,
        allow_private: bool,
        proxy_for: ProxyFor | None = None,
        proxied: Callable[[str], httpx.AsyncBaseTransport] | None = None,
        allow_unresolved_via_proxy: bool = False,
    ) -> None:
        self._direct = direct
        self._resolver = resolver
        self._allow_private = allow_private
        self._proxy_for = proxy_for or (lambda url: None)
        self._proxied = proxied
        self._allow_unresolved_via_proxy = allow_unresolved_via_proxy
        self._proxy_transports: dict[str, httpx.AsyncBaseTransport] = {}

    def _refuse(self, request: httpx.Request) -> FeedBlockedAddressError:
        return FeedBlockedAddressError("The feed address resolves to an address that cannot be used", request=request)

    def _via_proxy(self, proxy: str) -> httpx.AsyncBaseTransport:
        if self._proxied is None:
            return self._direct
        if proxy not in self._proxy_transports:
            self._proxy_transports[proxy] = self._proxied(proxy)
        return self._proxy_transports[proxy]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        proxy = self._proxy_for(str(request.url))
        route = self._via_proxy(proxy) if proxy else self._direct
        if is_noncanonical_numeric_host(host):
            raise self._refuse(request)
        try:
            literal: str | None = str(ipaddress.ip_address(host))
        except ValueError:
            literal = None
        if literal is not None:
            if is_forbidden_address(literal, allow_private=self._allow_private):
                raise self._refuse(request)
            return await route.handle_async_request(request)
        if self._resolver is None:
            return await route.handle_async_request(request)

        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        try:
            addresses = await self._resolver(host, port)
        except OSError:
            addresses = []
        if not addresses:
            if proxy and self._allow_unresolved_via_proxy:
                # The operator vouches that the proxy enforces egress rules.
                return await route.handle_async_request(request)
            if proxy:
                raise FeedUnresolvedError("The feed host does not resolve on this server", request=request)
            raise httpx.ConnectError("Could not resolve the feed host", request=request)
        if any(is_forbidden_address(a, allow_private=self._allow_private) for a in addresses):
            raise self._refuse(request)
        if proxy:
            # Checked, but the proxy resolves again: it must enforce egress rules itself.
            return await route.handle_async_request(request)

        extensions: dict[str, Any] = dict(request.extensions)
        if request.url.scheme == "https":
            extensions["sni_hostname"] = host
        last_error: httpx.ConnectError | httpx.ConnectTimeout | None = None
        for address in dict.fromkeys(a.split("%", 1)[0] for a in addresses):
            pinned = httpx.Request(
                request.method,
                request.url.copy_with(host=address),
                headers=request.headers,  # keeps the original Host header
                stream=request.stream,
                extensions=extensions,
            )
            try:
                return await self._direct.handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def aclose(self) -> None:
        await self._direct.aclose()
        for transport in self._proxy_transports.values():
            if transport is not self._direct:
                await transport.aclose()


def _environment_proxy(url: str) -> str | None:
    """The proxy ``HTTPS_PROXY`` / ``HTTP_PROXY`` name for *url*, unless ``NO_PROXY`` exempts its host."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if urllib.request.proxy_bypass_environment(host):  # type: ignore[attr-defined]
        return None
    return urllib.request.getproxies_environment().get(parts.scheme)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _decimal(value: Any, what: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise FeedError(f"Invalid rate in {what}")
    try:
        result = Decimal(str(value).strip()) if not isinstance(value, Decimal) else value
    except InvalidOperation:
        raise FeedError(f"Invalid rate in {what}") from None
    if not result.is_finite() or result <= 0:
        raise FeedError(f"Invalid rate in {what}")
    return result


def _code(value: Any, what: str) -> str:
    code = str(value or "").strip().upper()
    if not _CODE_RE.match(code):
        raise FeedError(f"Invalid currency code in {what}")
    return code


def _day(value: Any, what: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise FeedError(f"Invalid date in {what}") from None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def decode_feed_text(body: bytes) -> str:
    """The body as text: strict UTF-8 (a UTF-8 BOM is dropped), without control characters."""
    if body.startswith(_OTHER_BOMS):
        raise FeedError("The feed is not UTF-8 text")
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise FeedError("The feed is not UTF-8 text") from None
    if _CONTROL_CHARS.search(text):
        raise FeedError("The feed is not UTF-8 text")
    return text


def parse_ecb_xml(text: str) -> list[FeedRate]:
    """Rates from an ECB ``eurofxref`` XML document (daily, 90-day or full history)."""
    if _FORBIDDEN_XML.search(text):
        raise FeedError("The XML declares a DOCTYPE or entities, which is not accepted")
    declared = _XML_DECL_ENCODING.match(text)
    if declared and declared.group(1).strip().lower().replace("_", "-") not in ("utf-8", "utf8", "us-ascii", "ascii"):
        raise FeedError("The feed is not UTF-8 text")
    try:
        # Bytes, so the parser reads UTF-8 and never another declared encoding.
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as exc:
        logger.debug("Rate feed XML does not parse: %.200s", exc)
        raise FeedError("The response is not a valid ECB or Frankfurter feed") from None
    rates: list[FeedRate] = []
    item = 0
    for day_cube in root.iter():
        if _local(day_cube.tag) != "Cube" or "time" not in day_cube.attrib:
            continue
        day = _day(day_cube.attrib["time"], "a day entry")
        for cube in day_cube:
            if _local(cube.tag) != "Cube" or "currency" not in cube.attrib:
                continue
            item += 1
            what = f"item {item}"
            rates.append(
                FeedRate(day, "EUR", _code(cube.attrib["currency"], what), _decimal(cube.attrib.get("rate"), what))
            )
    if not rates:
        raise FeedError("The XML holds no euro reference rates")
    return rates


def _frankfurter_v1(data: dict[str, Any]) -> list[FeedRate]:
    base = _code(data.get("base"), "the base")
    amount = _decimal(data.get("amount", 1), "the amount")
    raw = data.get("rates")
    if not isinstance(raw, dict):
        raise FeedError("The response is not a valid ECB or Frankfurter feed")
    per_day: dict[str, Any] = raw if "date" not in data else {str(data["date"]): raw}
    rates: list[FeedRate] = []
    item = 0
    for day_text, day_rates in per_day.items():
        if not isinstance(day_rates, dict):
            raise FeedError("The response is not a valid ECB or Frankfurter feed")
        day = _day(day_text, "a day entry")
        for quote, value in day_rates.items():
            item += 1
            what = f"item {item}"
            rate = _decimal(value, what)
            rates.append(FeedRate(day, base, _code(quote, what), rate / amount if amount != 1 else rate))
    return rates


def _frankfurter_v2(rows: list[Any]) -> list[FeedRate]:
    rates: list[FeedRate] = []
    for index, row in enumerate(rows, start=1):
        what = f"item {index}"
        if not isinstance(row, dict):
            raise FeedError(f"Unexpected value in {what}")
        rates.append(
            FeedRate(
                _day(row.get("date"), what),
                _code(row.get("base"), what),
                _code(row.get("quote"), what),
                _decimal(row.get("rate"), what),
            )
        )
    return rates


def parse_feed(body: bytes) -> list[FeedRate]:
    """Rates from an ECB XML or Frankfurter JSON body; :class:`FeedError` otherwise."""
    text = decode_feed_text(body).lstrip(" \t\r\n")
    if text.startswith("<"):
        rates = parse_ecb_xml(text)
    elif text.startswith(("{", "[")):
        try:
            data = json.loads(text, parse_float=Decimal, parse_int=Decimal)
        except ValueError as exc:
            logger.debug("Rate feed JSON does not parse: %.200s", exc)
            raise FeedError("The response is not a valid ECB or Frankfurter feed") from None
        if isinstance(data, list):
            rates = _frankfurter_v2(data)
        elif isinstance(data, dict):
            if "message" in data and "rates" not in data:
                logger.debug("Rate feed answered with an error: %.200s", data.get("message"))
                raise FeedError("The feed returned an error")
            rates = _frankfurter_v1(data)
        else:
            raise FeedError("The response is not a valid ECB or Frankfurter feed")
    else:
        raise FeedError("The response is not a valid ECB or Frankfurter feed")
    rates = [r for r in rates if r.base != r.quote]
    if not rates:
        raise FeedError("The feed holds no rates")
    if len(rates) > MAX_FEED_RATES:
        raise FeedError(f"The feed holds more than {MAX_FEED_RATES} rates")
    return rates


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


async def fetch_feed(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
    allow_private: bool | None = None,
    allow_unresolved_via_proxy: bool | None = None,
    proxy_for: ProxyFor | None = None,
) -> list[FeedRate]:
    """Download *url* and parse it. Raises :class:`FeedError` on any failure.

    *transport* replaces the network (tests pass an ``httpx.MockTransport``,
    used for direct and proxied requests alike); with it, host names are
    checked only when a *resolver* is given too, and nothing is proxied unless
    *proxy_for* says so. *allow_private* and *allow_unresolved_via_proxy*
    default to ``EXCHANGE_RATE_FEED_ALLOW_PRIVATE_NETWORKS`` and
    ``EXCHANGE_RATE_FEED_ALLOW_UNRESOLVED_VIA_PROXY``.
    """
    try:
        url = validate_feed_url(url)
    except ValueError as exc:
        raise FeedError(str(exc)) from None
    if allow_private is None or allow_unresolved_via_proxy is None:
        from specivo.core.config import get_settings

        settings = get_settings()
        if allow_private is None:
            allow_private = settings.exchange_rate_feed_allow_private_networks
        if allow_unresolved_via_proxy is None:
            allow_unresolved_via_proxy = settings.exchange_rate_feed_allow_unresolved_via_proxy
    if transport is not None:
        mock = transport
        guarded = _FeedGuardTransport(
            mock,
            resolver=resolver,
            allow_private=allow_private,
            proxy_for=proxy_for,
            proxied=lambda proxy: mock,
            allow_unresolved_via_proxy=allow_unresolved_via_proxy,
        )
    else:
        guarded = _FeedGuardTransport(
            httpx.AsyncHTTPTransport(retries=0),
            resolver=resolver or _resolve_host,
            allow_private=allow_private,
            proxy_for=proxy_for or _environment_proxy,
            proxied=lambda proxy: httpx.AsyncHTTPTransport(retries=0, proxy=proxy),
            allow_unresolved_via_proxy=allow_unresolved_via_proxy,
        )
    try:
        async with asyncio.timeout(FEED_TIMEOUT_SECONDS):
            body = await _download(url, guarded)
    except FeedError:
        raise
    except TimeoutError:
        raise FeedError("The feed did not answer in time") from None
    except FeedBlockedAddressError:
        raise FeedError("The feed address or a redirect leads to an address that cannot be used") from None
    except FeedUnresolvedError:
        raise FeedError(
            "The feed host does not resolve on this server, so its address cannot be checked before "
            "the proxy is used (see EXCHANGE_RATE_FEED_ALLOW_UNRESOLVED_VIA_PROXY)"
        ) from None
    except httpx.TimeoutException:
        raise FeedError("The feed did not answer in time") from None
    except httpx.TooManyRedirects:
        raise FeedError("The feed redirected too many times") from None
    except httpx.HTTPError as exc:
        # No route on an air-gapped host, DNS failure, proxy refusal, TLS error.
        raise FeedError(f"Could not reach the feed: {type(exc).__name__}") from None
    return parse_feed(body)


async def _download(url: str, transport: httpx.AsyncBaseTransport) -> bytes:
    async with (
        httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(FEED_TIMEOUT_SECONDS, connect=10.0),
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            # Proxies are applied by fetch_feed itself, inside the guard.
            trust_env=False,
            headers={"Accept": "application/xml, application/json;q=0.9, */*;q=0.1"},
            # Each hop's scheme and form are checked before it is sent.
            event_hooks={"request": [_check_request]},
        ) as client,
        client.stream("GET", url) as response,
    ):
        if response.status_code != 200:
            raise FeedError(f"The feed answered HTTP {response.status_code}")
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_FEED_BYTES:
            raise FeedError(f"The feed is larger than {MAX_FEED_BYTES // (1024 * 1024)} MB")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_FEED_BYTES:
                raise FeedError(f"The feed is larger than {MAX_FEED_BYTES // (1024 * 1024)} MB")
            chunks.append(chunk)
    return b"".join(chunks)


async def _check_request(request: httpx.Request) -> None:
    try:
        validate_feed_url(str(request.url))
    except ValueError:
        raise FeedBlockedAddressError("The feed URL or a redirect cannot be used", request=request) from None
