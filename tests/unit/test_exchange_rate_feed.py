"""Reading ECB and Frankfurter rate feeds from recorded fixtures; fetching through a mocked transport.

The fixtures were recorded from the ECB daily XML and the public Frankfurter
API on 2026-11-05. No test here touches the network.
"""

from __future__ import annotations

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
        (b"", "neither"),
        (b"Service unavailable", "neither"),
        (b"<html><body>Proxy error</body></html>", "no euro reference rates"),
        (b"<Cube><Cube time='2026-11-05'><Cube currency='THB' rate='-1'/></Cube></Cube>", "positive"),
        (b"<Cube><Cube time='2026-11-05'><Cube currency='THB' rate='abc'/></Cube></Cube>", "not a number"),
        (b"<Cube><Cube time='yesterday'><Cube currency='THB' rate='1'/></Cube></Cube>", "not a date"),
        (b"<Cube><Cube time='2026-11-05'><Cube currency='baht' rate='1'/></Cube></Cube>", "currency code"),
        (b'{"status": 422, "message": "invalid currency"}', "error"),
        (b"[1, 2]", "not an object"),
        (b"{not json", "Not valid JSON"),
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
        "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml", transport=httpx.MockTransport(handler)
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
        await fetch_feed("https://rates.example.com/daily.xml", transport=httpx.MockTransport(handler))


async def test_fetch_refuses_a_redirect_to_a_metadata_address() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})

    with pytest.raises(FeedError, match="cannot be used"):
        await fetch_feed("https://rates.example.com/daily.xml", transport=httpx.MockTransport(handler))
    assert calls == ["https://rates.example.com/daily.xml"]


async def test_fetch_honours_the_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client reads HTTPS_PROXY / NO_PROXY from the environment (``trust_env``), like the model download."""
    created: list[dict] = []
    real_client = httpx.AsyncClient

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        created.append(kwargs)
        kwargs["transport"] = httpx.MockTransport(lambda r: httpx.Response(200, content=ECB_XML))
        return real_client(*args, **kwargs)

    monkeypatch.setattr("specivo.services.exchange_rate_feed.httpx.AsyncClient", spy)

    await fetch_feed("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml")

    assert created[0]["trust_env"] is True
