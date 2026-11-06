"""Reading ECB and Frankfurter rate feeds from recorded fixtures; fetching through a mocked transport.

The fixtures were recorded from the ECB daily XML and the public Frankfurter
API on 2026-11-05. No test here touches the network.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from specivo.services.exchange_rate_feed import (
    MAX_FEED_BYTES,
    FeedError,
    fetch_feed,
    parse_feed,
    validate_feed_url,
)

pytestmark = [pytest.mark.unit]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "exchange_rates"
ECB_XML = (FIXTURES / "ecb-eurofxref-daily-2026-11-05.xml").read_bytes()
FRANKFURTER_V1 = (FIXTURES / "frankfurter-v1-latest-2026-11-05.json").read_bytes()
FRANKFURTER_V2 = (FIXTURES / "frankfurter-v2-rates-ecb-2026-11-05.json").read_bytes()


def test_parses_the_ecb_daily_xml() -> None:
    rates = parse_feed(ECB_XML)

    assert len(rates) == 29
    assert {r.base for r in rates} == {"EUR"}
    assert {r.rate_date for r in rates} == {date(2026, 11, 5)}
    by_quote = {r.quote: r.rate for r in rates}
    assert by_quote["USD"] == Decimal("1.1224")
    assert by_quote["THB"] == Decimal("37.533")
    assert by_quote["PLN"] == Decimal("4.3890")
    assert all(type(r.rate) is Decimal for r in rates)


def test_parses_frankfurter_v1_and_v2_like_the_ecb() -> None:
    ecb = {(r.rate_date, r.quote): r.rate for r in parse_feed(ECB_XML)}
    v1 = {(r.rate_date, r.quote): r.rate for r in parse_feed(FRANKFURTER_V1)}
    v2 = {(r.rate_date, r.quote): r.rate for r in parse_feed(FRANKFURTER_V2)}

    assert v2 == ecb
    # Frankfurter v1 leaves the base out of its own rates; every other value matches.
    assert v1 == {k: v for k, v in ecb.items() if k[1] in {q for _, q in v1}}
    assert len(v1) >= 29
    assert all(type(v) is Decimal for v in v1.values())


def test_reads_several_days_from_a_history_file() -> None:
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
<Cube>
  <Cube time="2026-11-05"><Cube currency="THB" rate="37.533"/></Cube>
  <Cube time="2026-11-04"><Cube currency="THB" rate="37.4"/></Cube>
</Cube></gesmes:Envelope>"""

    rates = parse_feed(body)

    assert [(r.rate_date, r.rate) for r in rates] == [
        (date(2026, 11, 5), Decimal("37.533")),
        (date(2026, 11, 4), Decimal("37.4")),
    ]


def test_frankfurter_time_series_and_amount() -> None:
    body = (
        b'{"amount": 100, "base": "EUR", "start_date": "2026-11-04", "end_date": "2026-11-05",'
        b' "rates": {"2026-11-04": {"THB": 3740}, "2026-11-05": {"THB": 3753.3}}}'
    )

    rates = parse_feed(body)

    assert [(r.rate_date, r.rate) for r in rates] == [
        (date(2026, 11, 4), Decimal("37.4")),
        (date(2026, 11, 5), Decimal("37.533")),
    ]


@pytest.mark.parametrize(
    "body",
    [
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><Cube time="2026-11-05"/>',
        b'<!doctype x SYSTEM "file:///etc/passwd"><x/>',
    ],
)
def test_refuses_xml_with_a_doctype_or_entities(body: bytes) -> None:
    with pytest.raises(FeedError, match="DOCTYPE"):
        parse_feed(body)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"", "not a valid"),
        (b"Service unavailable", "not a valid"),
        (b"<html><body>Proxy error</body></html>", "no euro reference rates"),
        (b"<Cube><Cube time='2026-11-05'><Cube currency='THB' rate='-1'/></Cube></Cube>", "Invalid rate in item 1"),
        (b"<Cube><Cube time='2026-11-05'><Cube currency='THB' rate='abc'/></Cube></Cube>", "Invalid rate in item 1"),
        (b"<Cube><Cube time='yesterday'><Cube currency='THB' rate='1'/></Cube></Cube>", "Invalid date"),
        (
            b"<Cube><Cube time='2026-11-05'><Cube currency='baht' rate='1'/></Cube></Cube>",
            "Invalid currency code in item 1",
        ),
        (b'{"status": 422, "message": "invalid currency"}', "returned an error"),
        (b"[1, 2]", "Unexpected value in item 1"),
        (b"{not json", "not a valid"),
    ],
)
def test_bad_bodies_raise_feed_error(body: bytes, message: str) -> None:
    with pytest.raises(FeedError, match=message):
        parse_feed(body)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/rates.xml",
        "file:///etc/passwd",
        "https://user:secret@example.com/rates.xml",
        "http://169.254.169.254/latest/meta-data",
        "not a url",
    ],
)
def test_feed_url_validation(url: str) -> None:
    with pytest.raises(ValueError):
        validate_feed_url(url)


def test_feed_url_accepts_a_self_hosted_frankfurter() -> None:
    assert validate_feed_url(" http://frankfurter.internal:8080/v1/latest ") == (
        "http://frankfurter.internal:8080/v1/latest"
    )


async def test_fetch_reads_the_body_through_the_transport() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=ECB_XML, headers={"Content-Type": "text/xml"})

    rates = await fetch_feed(
        "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
        transport=httpx.MockTransport(handler),
        allow_private=False,
    )

    assert len(rates) == 29
    assert seen[0].url.host == "www.ecb.europa.eu"


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (lambda r: httpx.Response(503, content=b"down"), "HTTP 503"),
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("no route to host")), "Could not reach"),
        (lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("slow")), "in time"),
        (lambda r: (_ for _ in ()).throw(httpx.ProxyError("proxy said no")), "Could not reach"),
        (lambda r: httpx.Response(200, content=b"x" * (MAX_FEED_BYTES + 1)), "larger than"),
    ],
)
async def test_fetch_failures_become_feed_errors(handler, message: str) -> None:
    with pytest.raises(FeedError, match=message):
        await fetch_feed(
            "https://rates.example.com/daily.xml", transport=httpx.MockTransport(handler), allow_private=False
        )


async def test_fetch_refuses_a_redirect_to_a_metadata_address() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})

    with pytest.raises(FeedError, match="cannot be used"):
        await fetch_feed(
            "https://rates.example.com/daily.xml", transport=httpx.MockTransport(handler), allow_private=False
        )
    assert calls == ["https://rates.example.com/daily.xml"]


# ---------------------------------------------------------------------------
# Destinations: host names, redirects, numeric spellings, proxies
# ---------------------------------------------------------------------------


def _resolver(table: dict[str, list[str]]):  # type: ignore[no-untyped-def]
    async def resolve(host: str, port: int) -> list[str]:
        if host not in table:
            raise OSError("no such host")
        return table[host]

    return resolve


def _never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"request sent to {request.url}")


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "192.168.1.10", "169.254.169.254", "::1", "fd00::1"])
async def test_host_names_resolving_to_internal_addresses_are_refused(address: str) -> None:
    with pytest.raises(FeedError, match="cannot be used"):
        await fetch_feed(
            "https://rates.example.com/daily.xml",
            transport=httpx.MockTransport(_never_called),
            resolver=_resolver({"rates.example.com": ["93.184.215.14", address]}),
            allow_private=False,
        )


async def test_private_networks_can_be_allowed_but_metadata_never() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ECB_XML)

    rates = await fetch_feed(
        "http://frankfurter.internal/v1/latest",
        transport=httpx.MockTransport(handler),
        resolver=_resolver({"frankfurter.internal": ["10.0.0.5"]}),
        allow_private=True,
    )
    assert len(rates) == 29

    with pytest.raises(FeedError, match="cannot be used"):
        await fetch_feed(
            "http://metadata.internal/latest",
            transport=httpx.MockTransport(_never_called),
            resolver=_resolver({"metadata.internal": ["169.254.169.254"]}),
            allow_private=True,
        )


async def test_public_host_is_pinned_to_the_checked_address() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=ECB_XML)

    await fetch_feed(
        "https://rates.example.com/daily.xml",
        transport=httpx.MockTransport(handler),
        resolver=_resolver({"rates.example.com": ["93.184.215.14"]}),
        allow_private=False,
    )

    assert seen[0].url.host == "93.184.215.14"
    assert seen[0].headers["host"] == "rates.example.com"


async def test_redirect_to_an_internal_host_name_is_refused() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["host"])
        return httpx.Response(302, headers={"Location": "http://intranet.example.com/admin"})

    with pytest.raises(FeedError, match="cannot be used"):
        await fetch_feed(
            "https://rates.example.com/daily.xml",
            transport=httpx.MockTransport(handler),
            resolver=_resolver({"rates.example.com": ["93.184.215.14"], "intranet.example.com": ["10.0.0.7"]}),
            allow_private=False,
        )
    assert calls == ["rates.example.com"]


@pytest.mark.parametrize(
    "url", ["http://0x7f.1/rates.xml", "http://2130706433/rates.xml", "http://017.0.0.1/rates.xml", "http://127.1/"]
)
async def test_non_canonical_numeric_hosts_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        validate_feed_url(url)
    with pytest.raises(FeedError, match="cannot be used"):
        await fetch_feed(url, transport=httpx.MockTransport(_never_called), allow_private=True)


def test_ordinary_host_names_with_hex_letters_are_not_numeric() -> None:
    assert validate_feed_url("https://cafe.ca/rates.xml") == "https://cafe.ca/rates.xml"


async def test_proxy_comes_from_the_environment_and_the_guard_still_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    from specivo.services import exchange_rate_feed as feed

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", "frankfurter.internal")
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    assert feed._environment_proxy("https://www.ecb.europa.eu/x.xml") == "http://proxy.internal:3128"
    assert feed._environment_proxy("https://frankfurter.internal/v1/latest") is None

    # Through the proxy, a target that resolves to an internal address is still refused,
    # before anything is sent (the proxy address is never contacted).
    with pytest.raises(FeedError, match="cannot be used"):
        await fetch_feed(
            "https://rates.example.com/daily.xml",
            resolver=_resolver({"rates.example.com": ["10.9.9.9"]}),
            allow_private=False,
        )


# ---------------------------------------------------------------------------
# Encodings and XML declarations
# ---------------------------------------------------------------------------


_XXE = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><Cube time="2026-11-05"/>'


@pytest.mark.parametrize(
    "body",
    [
        _XXE.encode("utf-16"),  # with a BOM
        _XXE.encode("utf-16-le"),  # without a BOM: NUL bytes
        _XXE.encode("utf-16-be"),
        _XXE.encode("utf-32"),
        b'<?xml version="1.0" encoding="UTF-16"?><Cube/>',
        b'<?xml version="1.0" encoding="ISO-8859-1"?><Cube/>',
        b"<Cube time='2026-11-05'>\xff\xfe</Cube>",
    ],
)
def test_bodies_in_other_encodings_are_refused(body: bytes) -> None:
    with pytest.raises(FeedError, match="UTF-8|DOCTYPE"):
        parse_feed(body)


def test_utf8_bom_and_declaration_are_fine() -> None:
    assert len(parse_feed(b"\xef\xbb\xbf" + ECB_XML)) == 29


# ---------------------------------------------------------------------------
# Error messages never carry response content
# ---------------------------------------------------------------------------

MARKER = "INTERNAL-SECRET-7f3a"


@pytest.mark.parametrize(
    "body",
    [
        f'{{"status": 500, "message": "{MARKER}"}}',
        f'{{"amount": 1, "base": "EUR", "date": "2026-11-05", "rates": {{"THB": "{MARKER}"}}}}',
        f'{{"amount": 1, "base": "EUR", "date": "2026-11-05", "rates": {{"{MARKER}": 1}}}}',
        f'{{"amount": 1, "base": "EUR", "date": "{MARKER}", "rates": {{"THB": 1}}}}',
        f'[{{"date": "2026-11-05", "base": "EUR", "quote": "THB", "rate": "{MARKER}"}}]',
        f"<Cube><Cube time='2026-11-05'><Cube currency='{MARKER}' rate='1'/></Cube></Cube>",
        f"<Cube><Cube time='2026-11-05'><Cube currency='THB' rate='{MARKER}'/></Cube></Cube>",
        f"<Cube><{MARKER}></Cube>",
        f"{{{MARKER}",
        MARKER,
    ],
)
def test_error_messages_hold_no_response_content(body: str) -> None:
    with pytest.raises(FeedError) as caught:
        parse_feed(body.encode())
    assert MARKER not in str(caught.value)
    assert MARKER.lower() not in str(caught.value).lower()


# ---------------------------------------------------------------------------
# Total deadline
# ---------------------------------------------------------------------------


class _DripStream(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[no-untyped-def]
        while True:
            yield b"<"
            await asyncio.sleep(0.05)


async def test_a_slow_drip_is_cut_off_at_the_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    from specivo.services import exchange_rate_feed as feed

    monkeypatch.setattr(feed, "FEED_TIMEOUT_SECONDS", 0.3)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, stream=_DripStream()))

    started = time.monotonic()
    with pytest.raises(FeedError, match="in time"):
        await fetch_feed("https://rates.example.com/daily.xml", transport=transport, allow_private=False)
    assert time.monotonic() - started < 2
