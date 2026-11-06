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

Numbers are read as ``Decimal`` straight from the text (``json`` with
``parse_float=Decimal``), never through a float. XML with a DOCTYPE or entity
declaration is refused before it reaches the parser.

The HTTP client honours the standard proxy variables (``HTTPS_PROXY``,
``NO_PROXY``) of the process, like the model download does. Every failure
(no network on an air-gapped host, a proxy refusing, a bad body) raises
:class:`FeedError` with a short message for the admin page; nothing is retried
until the next scheduled run.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import httpx

#: The ECB's daily euro reference rates (about 30 currencies, updated around 16:00 CET on working days).
DEFAULT_FEED_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

#: Largest body accepted. The full ECB history since 1999 is about 6 MB.
MAX_FEED_BYTES = 8 * 1024 * 1024

#: Most rates one fetch may store.
MAX_FEED_RATES = 250_000

FEED_TIMEOUT_SECONDS = 20.0
MAX_REDIRECTS = 3

_CODE_RE = re.compile(r"^[A-Z]{3}$")
_FORBIDDEN_XML = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


class FeedError(Exception):
    """The feed could not be fetched or read. The message is shown to administrators."""


@dataclass(frozen=True, slots=True)
class FeedRate:
    """``1 base = rate quote`` on ``rate_date``, as the feed published it."""

    rate_date: date
    base: str
    quote: str
    rate: Decimal


def validate_feed_url(url: str) -> str:
    """Return *url* stripped, or raise ``ValueError`` when it is not a usable http(s) URL.

    Link-local and cloud metadata addresses written as IP literals are refused,
    as for single sign-on.
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
    try:
        blocked = is_blocked_address(parts.hostname)
    except ValueError:
        blocked = False  # a host name, not an IP literal
    if blocked:
        raise ValueError("That address cannot be used")
    return text


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _decimal(value: Any, where: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise FeedError(f"{where}: rate is not a decimal number")
    try:
        result = Decimal(str(value).strip()) if not isinstance(value, Decimal) else value
    except InvalidOperation:
        raise FeedError(f"{where}: rate {value!r} is not a number") from None
    if not result.is_finite() or result <= 0:
        raise FeedError(f"{where}: rate {value!r} is not a positive number")
    return result


def _code(value: Any, where: str) -> str:
    code = str(value or "").strip().upper()
    if not _CODE_RE.match(code):
        raise FeedError(f"{where}: {value!r} is not a currency code")
    return code


def _day(value: Any, where: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise FeedError(f"{where}: {value!r} is not a date") from None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_ecb_xml(body: bytes) -> list[FeedRate]:
    """Rates from an ECB ``eurofxref`` XML document (daily, 90-day or full history)."""
    if _FORBIDDEN_XML.search(body):
        raise FeedError("The XML declares a DOCTYPE or entities, which is not accepted")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FeedError(f"Not valid XML: {exc}") from None
    rates: list[FeedRate] = []
    for day_cube in root.iter():
        if _local(day_cube.tag) != "Cube" or "time" not in day_cube.attrib:
            continue
        day = _day(day_cube.attrib["time"], "XML")
        for cube in day_cube:
            if _local(cube.tag) != "Cube" or "currency" not in cube.attrib:
                continue
            where = f"XML {day.isoformat()}"
            rates.append(
                FeedRate(day, "EUR", _code(cube.attrib["currency"], where), _decimal(cube.attrib.get("rate"), where))
            )
    if not rates:
        raise FeedError("The XML holds no euro reference rates")
    return rates


def _frankfurter_v1(data: dict[str, Any]) -> list[FeedRate]:
    base = _code(data.get("base"), "JSON")
    amount = _decimal(data.get("amount", 1), "JSON amount")
    raw = data.get("rates")
    if not isinstance(raw, dict):
        raise FeedError("JSON: 'rates' is missing")
    per_day: dict[str, Any] = raw if "date" not in data else {str(data["date"]): raw}
    rates: list[FeedRate] = []
    for day_text, day_rates in per_day.items():
        if not isinstance(day_rates, dict):
            raise FeedError("JSON: unexpected 'rates' shape")
        day = _day(day_text, "JSON")
        for quote, value in day_rates.items():
            rate = _decimal(value, f"JSON {day_text} {quote}")
            rates.append(FeedRate(day, base, _code(quote, "JSON"), rate / amount if amount != 1 else rate))
    return rates


def _frankfurter_v2(rows: list[Any]) -> list[FeedRate]:
    rates: list[FeedRate] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise FeedError(f"JSON item {index}: not an object")
        where = f"JSON item {index}"
        rates.append(
            FeedRate(
                _day(row.get("date"), where),
                _code(row.get("base"), where),
                _code(row.get("quote"), where),
                _decimal(row.get("rate"), where),
            )
        )
    return rates


def parse_feed(body: bytes) -> list[FeedRate]:
    """Rates from an ECB XML or Frankfurter JSON body; :class:`FeedError` otherwise."""
    text = body.lstrip(b"\xef\xbb\xbf \t\r\n")
    if text.startswith(b"<"):
        rates = parse_ecb_xml(text)
    elif text.startswith((b"{", b"[")):
        try:
            data = json.loads(text, parse_float=Decimal, parse_int=Decimal)
        except ValueError as exc:
            raise FeedError(f"Not valid JSON: {exc}") from None
        if isinstance(data, list):
            rates = _frankfurter_v2(data)
        elif isinstance(data, dict):
            if "message" in data and "rates" not in data:
                raise FeedError(f"The feed answered with an error: {str(data['message'])[:200]}")
            rates = _frankfurter_v1(data)
        else:
            raise FeedError("JSON: unexpected document")
    else:
        raise FeedError("The response is neither ECB XML nor Frankfurter JSON")
    rates = [r for r in rates if r.base != r.quote]
    if not rates:
        raise FeedError("The feed holds no rates")
    if len(rates) > MAX_FEED_RATES:
        raise FeedError(f"The feed holds more than {MAX_FEED_RATES} rates")
    return rates


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


async def fetch_feed(url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> list[FeedRate]:
    """Download *url* and parse it. Raises :class:`FeedError` on any failure.

    *transport* replaces the network (tests pass an ``httpx.MockTransport``).
    """
    try:
        url = validate_feed_url(url)
    except ValueError as exc:
        raise FeedError(str(exc)) from None
    timeout = httpx.Timeout(FEED_TIMEOUT_SECONDS, connect=10.0)
    try:
        async with (
            httpx.AsyncClient(
                transport=transport,
                timeout=timeout,
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                # Proxies come from HTTPS_PROXY / NO_PROXY, as for the model download.
                trust_env=True,
                headers={"Accept": "application/xml, application/json;q=0.9, */*;q=0.1"},
                # Every request, redirects included, is checked before it is sent.
                event_hooks={"request": [_check_request]},
            ) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                raise FeedError(f"The feed answered HTTP {response.status_code}")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_FEED_BYTES:
                    raise FeedError(f"The feed is larger than {MAX_FEED_BYTES // (1024 * 1024)} MB")
                chunks.append(chunk)
    except FeedError:
        raise
    except httpx.TimeoutException:
        raise FeedError("The feed did not answer in time") from None
    except httpx.TooManyRedirects:
        raise FeedError("The feed redirected too many times") from None
    except httpx.HTTPError as exc:
        # No route on an air-gapped host, DNS failure, proxy refusal, TLS error.
        raise FeedError(f"Could not reach the feed: {type(exc).__name__}") from None
    return parse_feed(b"".join(chunks))


async def _check_request(request: httpx.Request) -> None:
    try:
        validate_feed_url(str(request.url))
    except ValueError as exc:
        raise FeedError(f"The feed URL or a redirect cannot be used: {exc}") from None
