"""Unit tests for the SSO HTTP client factory and the SSO settings.

Every request to an identity provider goes through
``specivo.services.sso.http.create_sso_client``. These tests pin its limits:
timeout, response size cap, HTTPS enforcement, same-host redirects only, and
the transport seam the mock IdP uses.
"""

from __future__ import annotations

import asyncio
import gzip
import os
import time

import httpx
import pytest

from specivo.core.config import Settings, get_settings
from specivo.services.sso import http as sso_http
from specivo.services.sso.http import (
    MAX_RESPONSE_BYTES,
    SSOBlockedAddressError,
    SSODeadlineExceededError,
    SSOHttpError,
    SSOInsecureURLError,
    SSORedirectError,
    SSOResponseTooLargeError,
    create_sso_client,
    is_blocked_address,
    override_transport,
)

pytestmark = pytest.mark.unit


def _settings(**overrides: object) -> Settings:
    return get_settings().model_copy(update=overrides)


def _new_settings(**env: object) -> Settings:
    """Build Settings through validation (unlike ``model_copy``)."""
    return Settings(
        database_url=os.environ.get("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db"),
        redis_url="redis://localhost:6379/0",
        secret_key="x" * 40,
        **env,  # type: ignore[arg-type]
    )


class _Recorder:
    """A MockTransport handler that records the URLs it was asked for."""

    def __init__(self, routes: dict[str, httpx.Response] | None = None) -> None:
        self.routes = routes or {}
        self.seen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(str(request.url))
        return self.routes.get(str(request.url), httpx.Response(200, json={"ok": True}))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSsoSettings:
    def test_defaults(self) -> None:
        s = _new_settings()
        assert s.sso_http_timeout_seconds == 5
        assert s.sso_allow_insecure_http is False
        assert s.sso_clock_skew_seconds == 60
        assert s.sso_force_local_login is False

    def test_clock_skew_is_capped_at_300(self) -> None:
        assert _new_settings(sso_clock_skew_seconds=1000).sso_clock_skew_seconds == 300

    def test_clock_skew_below_cap_is_kept(self) -> None:
        assert _new_settings(sso_clock_skew_seconds=120).sso_clock_skew_seconds == 120

    def test_negative_clock_skew_rejected(self) -> None:
        with pytest.raises(ValueError):
            _new_settings(sso_clock_skew_seconds=-1)

    def test_non_positive_timeout_rejected(self) -> None:
        with pytest.raises(ValueError):
            _new_settings(sso_http_timeout_seconds=0)


# ---------------------------------------------------------------------------
# Client configuration
# ---------------------------------------------------------------------------


class TestClientConfiguration:
    async def test_timeout_comes_from_settings(self) -> None:
        async with create_sso_client(_settings(sso_http_timeout_seconds=2.5)) as client:
            assert client.timeout.connect == 2.5
            assert client.timeout.read == 2.5
            assert client.timeout.pool == 2.5

    async def test_default_timeout_is_five_seconds(self) -> None:
        async with create_sso_client(_settings(sso_http_timeout_seconds=5.0)) as client:
            assert client.timeout.read == 5.0

    async def test_errors_are_httpx_request_errors(self) -> None:
        """Callers map one exception family to ``sso_idp_unreachable``."""
        assert issubclass(SSOHttpError, httpx.RequestError)
        for exc in (SSOInsecureURLError, SSORedirectError, SSOResponseTooLargeError):
            assert issubclass(exc, SSOHttpError)


# ---------------------------------------------------------------------------
# Transport seam
# ---------------------------------------------------------------------------


class TestTransportOverride:
    async def test_override_routes_requests_to_the_given_transport(self) -> None:
        recorder = _Recorder()
        with override_transport(httpx.MockTransport(recorder)):
            async with create_sso_client(_settings()) as client:
                resp = await client.get("https://idp.example.com/jwks")
        assert resp.json() == {"ok": True}
        assert recorder.seen == ["https://idp.example.com/jwks"]

    async def test_override_is_removed_on_exit(self) -> None:
        with override_transport(httpx.MockTransport(_Recorder())):
            pass
        assert sso_http.get_transport_override() is None

    async def test_override_is_removed_when_the_block_raises(self) -> None:
        with pytest.raises(RuntimeError):
            with override_transport(httpx.MockTransport(_Recorder())):
                raise RuntimeError("boom")
        assert sso_http.get_transport_override() is None

    async def test_nested_override_restores_the_outer_one(self) -> None:
        outer = httpx.MockTransport(_Recorder())
        inner = httpx.MockTransport(_Recorder())
        with override_transport(outer):
            with override_transport(inner):
                assert sso_http.get_transport_override() is inner
            assert sso_http.get_transport_override() is outer


# ---------------------------------------------------------------------------
# HTTPS enforcement
# ---------------------------------------------------------------------------


class TestHttpsEnforcement:
    async def test_plain_http_is_refused_before_sending(self) -> None:
        recorder = _Recorder()
        with override_transport(httpx.MockTransport(recorder)):
            async with create_sso_client(_settings(sso_allow_insecure_http=False)) as client:
                with pytest.raises(SSOInsecureURLError):
                    await client.get("http://idp.example.com/.well-known/openid-configuration")
        assert recorder.seen == []

    async def test_plain_http_allowed_when_insecure_setting_on(self) -> None:
        recorder = _Recorder()
        with override_transport(httpx.MockTransport(recorder)):
            async with create_sso_client(_settings(sso_allow_insecure_http=True)) as client:
                resp = await client.get("http://idp.example.com/jwks")
        assert resp.status_code == 200

    async def test_redirect_to_http_is_refused(self) -> None:
        recorder = _Recorder(
            {"https://idp.example.com/a": httpx.Response(302, headers={"Location": "http://idp.example.com/b"})}
        )
        with override_transport(httpx.MockTransport(recorder)):
            async with create_sso_client(_settings(sso_allow_insecure_http=False)) as client:
                with pytest.raises(SSOInsecureURLError):
                    await client.get("https://idp.example.com/a")
        assert recorder.seen == ["https://idp.example.com/a"]


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------


class TestRedirects:
    async def test_same_host_redirect_is_followed(self) -> None:
        recorder = _Recorder(
            {
                "https://idp.example.com/old": httpx.Response(301, headers={"Location": "https://idp.example.com/new"}),
                "https://idp.example.com/new": httpx.Response(200, json={"moved": True}),
            }
        )
        with override_transport(httpx.MockTransport(recorder)):
            async with create_sso_client(_settings()) as client:
                resp = await client.get("https://idp.example.com/old")
        assert resp.json() == {"moved": True}

    async def test_cross_host_redirect_is_refused(self) -> None:
        recorder = _Recorder(
            {"https://idp.example.com/jwks": httpx.Response(302, headers={"Location": "https://attacker.example.net/"})}
        )
        with override_transport(httpx.MockTransport(recorder)):
            async with create_sso_client(_settings()) as client:
                with pytest.raises(SSORedirectError):
                    await client.get("https://idp.example.com/jwks")
        assert recorder.seen == ["https://idp.example.com/jwks"]

    async def test_cross_port_redirect_is_refused(self) -> None:
        recorder = _Recorder(
            {"https://idp.example.com/jwks": httpx.Response(302, headers={"Location": "https://idp.example.com:8443/"})}
        )
        with override_transport(httpx.MockTransport(recorder)):
            async with create_sso_client(_settings()) as client:
                with pytest.raises(SSORedirectError):
                    await client.get("https://idp.example.com/jwks")

    async def test_redirect_loop_is_bounded(self) -> None:
        recorder = _Recorder(
            {"https://idp.example.com/loop": httpx.Response(302, headers={"Location": "https://idp.example.com/loop"})}
        )
        with override_transport(httpx.MockTransport(recorder)):
            async with create_sso_client(_settings()) as client:
                with pytest.raises(httpx.TooManyRedirects):
                    await client.get("https://idp.example.com/loop")
        assert len(recorder.seen) <= 6


# ---------------------------------------------------------------------------
# Response size cap
# ---------------------------------------------------------------------------


class TestResponseSizeCap:
    async def test_cap_is_one_megabyte(self) -> None:
        assert MAX_RESPONSE_BYTES == 1024 * 1024

    async def test_body_at_the_cap_is_accepted(self) -> None:
        body = b"a" * MAX_RESPONSE_BYTES
        recorder = _Recorder({"https://idp.example.com/x": httpx.Response(200, content=body)})
        with override_transport(httpx.MockTransport(recorder)):
            async with create_sso_client(_settings()) as client:
                resp = await client.get("https://idp.example.com/x")
        assert len(resp.content) == MAX_RESPONSE_BYTES

    async def test_declared_oversized_body_is_refused(self) -> None:
        body = b"a" * (MAX_RESPONSE_BYTES + 1)
        recorder = _Recorder({"https://idp.example.com/x": httpx.Response(200, content=body)})
        with override_transport(httpx.MockTransport(recorder)):
            async with create_sso_client(_settings()) as client:
                with pytest.raises(SSOResponseTooLargeError):
                    await client.get("https://idp.example.com/x")

    async def test_streamed_oversized_body_without_length_is_refused(self) -> None:
        async def chunks():
            for _ in range(20):
                yield b"a" * 100_000

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=chunks())

        with override_transport(httpx.MockTransport(handler)):
            async with create_sso_client(_settings()) as client:
                with pytest.raises(SSOResponseTooLargeError):
                    await client.get("https://idp.example.com/x")

    async def test_lying_content_length_does_not_bypass_the_cap(self) -> None:
        async def chunks():
            for _ in range(20):
                yield b"a" * 100_000

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"Content-Length": "10"}, content=chunks())

        with override_transport(httpx.MockTransport(handler)):
            async with create_sso_client(_settings()) as client:
                with pytest.raises((SSOResponseTooLargeError, httpx.HTTPError)):
                    await client.get("https://idp.example.com/x")

    async def test_compressed_body_is_refused(self) -> None:
        """A small gzip body could inflate far past the cap, so it is not accepted."""
        recorder = _Recorder(
            {
                "https://idp.example.com/x": httpx.Response(
                    200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(b"{}")
                )
            }
        )
        with override_transport(httpx.MockTransport(recorder)):
            async with create_sso_client(_settings()) as client:
                with pytest.raises(SSOHttpError):
                    await client.get("https://idp.example.com/x")

    async def test_client_asks_for_uncompressed_bodies(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["Accept-Encoding"])
            return httpx.Response(200)

        with override_transport(httpx.MockTransport(handler)):
            async with create_sso_client(_settings()) as client:
                await client.get("https://idp.example.com/x")
        assert seen == ["identity"]


# ---------------------------------------------------------------------------
# Total deadline
# ---------------------------------------------------------------------------


async def _slow_chunks(count: int, delay: float):
    for _ in range(count):
        await asyncio.sleep(delay)
        yield b"a"


class TestTotalDeadline:
    """``httpx.Timeout`` limits each phase; the deadline limits the whole request."""

    async def test_slow_drip_body_hits_the_deadline(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_slow_chunks(50, 0.02))

        with override_transport(httpx.MockTransport(handler)):
            async with create_sso_client(_settings(sso_http_timeout_seconds=0.2)) as client:
                started = time.monotonic()
                with pytest.raises(SSODeadlineExceededError):
                    await client.get("https://idp.example.com/x")
        assert time.monotonic() - started < 0.5

    async def test_slow_drip_body_hits_the_deadline_when_streaming(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_slow_chunks(50, 0.02))

        with override_transport(httpx.MockTransport(handler)):
            async with create_sso_client(_settings(sso_http_timeout_seconds=0.2)) as client:
                with pytest.raises(SSODeadlineExceededError):
                    async with client.stream("GET", "https://idp.example.com/x") as resp:
                        await resp.aread()

    async def test_slow_response_hits_the_deadline(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(1)
            return httpx.Response(200)

        with override_transport(httpx.MockTransport(handler)):
            async with create_sso_client(_settings(sso_http_timeout_seconds=0.1)) as client:
                with pytest.raises(SSODeadlineExceededError):
                    await client.get("https://idp.example.com/x")

    async def test_deadline_spans_redirect_hops(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.08)
            if request.url.path == "/a":
                return httpx.Response(302, headers={"Location": "/b"})
            if request.url.path == "/b":
                return httpx.Response(302, headers={"Location": "/c"})
            return httpx.Response(200)

        with override_transport(httpx.MockTransport(handler)):
            async with create_sso_client(_settings(sso_http_timeout_seconds=0.2)) as client:
                with pytest.raises(SSODeadlineExceededError):
                    await client.get("https://idp.example.com/a")

    async def test_fast_response_is_unaffected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_slow_chunks(3, 0.01))

        with override_transport(httpx.MockTransport(handler)):
            async with create_sso_client(_settings(sso_http_timeout_seconds=0.5)) as client:
                assert (await client.get("https://idp.example.com/x")).content == b"aaa"

    async def test_deadline_error_is_an_httpx_timeout(self) -> None:
        assert issubclass(SSODeadlineExceededError, httpx.TimeoutException)
        assert issubclass(SSODeadlineExceededError, SSOHttpError)


# ---------------------------------------------------------------------------
# Link-local / cloud metadata destinations
# ---------------------------------------------------------------------------

BLOCKED_ADDRESSES = [
    "169.254.169.254",
    "169.254.0.1",
    "169.254.255.255",
    "100.100.100.200",
    "fe80::1",
    "febf:ffff::1",
    "fd00:ec2::254",
    "::ffff:169.254.169.254",
    "64:ff9b::a9fe:a9fe",
]

ALLOWED_ADDRESSES = [
    "10.0.0.5",
    "172.16.1.1",
    "192.168.1.10",
    "127.0.0.1",
    "::1",
    "fd00:ec2::253",
    "fc00::1",
    "93.184.216.34",
    "2606:2800:220:1::1",
    "169.253.255.255",
    "169.255.0.0",
]


class TestBlockedAddresses:
    @pytest.mark.parametrize("address", BLOCKED_ADDRESSES)
    def test_blocked(self, address: str) -> None:
        assert is_blocked_address(address)

    @pytest.mark.parametrize("address", ALLOWED_ADDRESSES)
    def test_allowed(self, address: str) -> None:
        """Private and loopback ranges stay usable: internal IdPs are the main use case."""
        assert not is_blocked_address(address)

    def test_ipv6_scope_id_is_ignored(self) -> None:
        assert is_blocked_address("fe80::1%eth0")

    @pytest.mark.parametrize(
        "url",
        [
            "https://169.254.169.254/latest/meta-data/",
            "https://[fe80::1]/x",
            "https://[fd00:ec2::254]/x",
            "https://[::ffff:169.254.169.254]/x",
        ],
    )
    async def test_ip_literal_refused_through_the_seam(self, url: str) -> None:
        recorder = _Recorder()
        with override_transport(httpx.MockTransport(recorder)):
            async with create_sso_client(_settings(sso_allow_insecure_http=True)) as client:
                with pytest.raises(SSOBlockedAddressError):
                    await client.get(url)
        assert recorder.seen == []

    async def test_http_metadata_refused_even_with_insecure_http(self) -> None:
        recorder = _Recorder()
        with override_transport(httpx.MockTransport(recorder)):
            async with create_sso_client(_settings(sso_allow_insecure_http=True)) as client:
                with pytest.raises(SSOBlockedAddressError):
                    await client.get("http://169.254.169.254/latest/meta-data/")
        assert recorder.seen == []

    async def test_redirect_to_metadata_ip_is_refused(self) -> None:
        """A same-host check alone would not catch it if the IdP host were the IP itself."""
        recorder = _Recorder(
            {
                "https://10.0.0.5/jwks": httpx.Response(
                    302, headers={"Location": "https://169.254.169.254/latest/meta-data/"}
                )
            }
        )
        with override_transport(httpx.MockTransport(recorder)):
            async with create_sso_client(_settings()) as client:
                with pytest.raises((SSOBlockedAddressError, SSORedirectError)):
                    await client.get("https://10.0.0.5/jwks")
        assert recorder.seen == ["https://10.0.0.5/jwks"]

    async def test_seam_does_not_resolve_hostnames(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _boom(host: str, port: int) -> list[str]:
            raise AssertionError("the test seam must not resolve DNS")

        monkeypatch.setattr(sso_http, "_resolve_host", _boom)
        with override_transport(httpx.MockTransport(_Recorder())):
            async with create_sso_client(_settings()) as client:
                assert (await client.get("https://idp.example.com/x")).status_code == 200


class TestResolutionAndPinning:
    """The real-network path resolves once, checks every address and pins the connection."""

    @staticmethod
    def _client(resolved: dict[str, list[str]], seen: list[httpx.Request]) -> httpx.AsyncClient:
        async def resolver(host: str, port: int) -> list[str]:
            return resolved[host]

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/hop":
                return httpx.Response(302, headers={"Location": "https://idp.example.com/end"})
            return httpx.Response(200)

        transport = sso_http._GuardedTransport(httpx.MockTransport(handler), resolver=resolver)
        return httpx.AsyncClient(transport=transport)

    async def test_connection_is_pinned_to_the_checked_address(self) -> None:
        seen: list[httpx.Request] = []
        async with self._client({"idp.example.com": ["93.184.216.34"]}, seen) as client:
            resp = await client.get("https://idp.example.com/x")
        assert resp.status_code == 200
        (sent,) = seen
        assert sent.url.host == "93.184.216.34"
        assert sent.headers["Host"] == "idp.example.com"
        assert sent.extensions["sni_hostname"] == "idp.example.com"
        assert resp.request.url.host == "idp.example.com"

    async def test_ipv6_answer_is_pinned(self) -> None:
        seen: list[httpx.Request] = []
        async with self._client({"idp.example.com": ["2606:2800:220:1::1"]}, seen) as client:
            await client.get("https://idp.example.com/x")
        assert seen[0].url.host == "2606:2800:220:1::1"

    async def test_metadata_answer_is_refused(self) -> None:
        seen: list[httpx.Request] = []
        async with self._client({"idp.example.com": ["169.254.169.254"]}, seen) as client:
            with pytest.raises(SSOBlockedAddressError):
                await client.get("https://idp.example.com/x")
        assert seen == []

    async def test_any_blocked_answer_refuses_the_host(self) -> None:
        seen: list[httpx.Request] = []
        async with self._client({"idp.example.com": ["93.184.216.34", "169.254.169.254"]}, seen) as client:
            with pytest.raises(SSOBlockedAddressError):
                await client.get("https://idp.example.com/x")
        assert seen == []

    async def test_private_answer_is_allowed(self) -> None:
        seen: list[httpx.Request] = []
        async with self._client({"idp.internal.example.com": ["10.1.2.3"]}, seen) as client:
            assert (await client.get("https://idp.internal.example.com/x")).status_code == 200

    async def test_every_redirect_hop_is_resolved_again(self) -> None:
        calls: list[str] = []
        answers = iter([["93.184.216.34"], ["169.254.169.254"]])

        async def resolver(host: str, port: int) -> list[str]:
            calls.append(host)
            return next(answers)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "https://idp.example.com/end"})

        transport = sso_http._GuardedTransport(httpx.MockTransport(handler), resolver=resolver)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            with pytest.raises(SSOBlockedAddressError):
                await client.get("https://idp.example.com/hop")
        assert calls == ["idp.example.com", "idp.example.com"]

    @staticmethod
    def _failing_client(answers: list[str], failing: dict[str, Exception], seen: list[str]) -> httpx.AsyncClient:
        async def resolver(host: str, port: int) -> list[str]:
            return answers

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.host)
            error = failing.get(request.url.host)
            if error is not None:
                raise error
            return httpx.Response(200, text=request.content.decode() or "ok")

        transport = sso_http._GuardedTransport(httpx.MockTransport(handler), resolver=resolver)
        return httpx.AsyncClient(transport=transport)

    async def test_connect_failure_falls_back_to_the_next_address(self) -> None:
        """``localhost`` answers ::1 first; an IdP listening on IPv4 only is still reached."""
        seen: list[str] = []
        failing = {"::1": httpx.ConnectError("connection refused")}
        async with self._failing_client(["::1", "127.0.0.1"], failing, seen) as client:
            resp = await client.post("http://localhost:8033/token", content=b"grant_type=authorization_code")
        assert resp.status_code == 200
        # The body is sent again, complete, to the second address.
        assert resp.text == "grant_type=authorization_code"
        assert seen == ["::1", "127.0.0.1"]

    async def test_connect_timeout_falls_back_to_the_next_address(self) -> None:
        seen: list[str] = []
        failing = {"2606:2800:220:1::1": httpx.ConnectTimeout("no route")}
        async with self._failing_client(["2606:2800:220:1::1", "93.184.216.34"], failing, seen) as client:
            assert (await client.get("https://idp.example.com/x")).status_code == 200
        assert seen == ["2606:2800:220:1::1", "93.184.216.34"]

    async def test_every_address_failing_raises_the_last_connect_error(self) -> None:
        seen: list[str] = []
        failing: dict[str, Exception] = {
            "::1": httpx.ConnectError("refused v6"),
            "127.0.0.1": httpx.ConnectError("refused v4"),
        }
        async with self._failing_client(["::1", "127.0.0.1"], failing, seen) as client:
            with pytest.raises(httpx.ConnectError, match="refused v4"):
                await client.get("https://localhost/x")
        assert seen == ["::1", "127.0.0.1"]

    async def test_failure_after_connecting_is_not_retried(self) -> None:
        """Only connection failures fall back: once a request may have been sent, it is not resent."""
        seen: list[str] = []
        failing = {"::1": httpx.ReadError("connection reset")}
        async with self._failing_client(["::1", "127.0.0.1"], failing, seen) as client:
            with pytest.raises(httpx.ReadError):
                await client.get("https://localhost/x")
        assert seen == ["::1"]

    async def test_duplicate_answers_are_tried_once(self) -> None:
        seen: list[str] = []
        failing = {"::1": httpx.ConnectError("refused")}
        async with self._failing_client(["::1", "::1", "127.0.0.1"], failing, seen) as client:
            assert (await client.get("https://localhost/x")).status_code == 200
        assert seen == ["::1", "127.0.0.1"]

    async def test_resolution_failure_is_a_connect_error(self) -> None:
        async def resolver(host: str, port: int) -> list[str]:
            raise OSError("no such host")

        transport = sso_http._GuardedTransport(httpx.MockTransport(lambda r: httpx.Response(200)), resolver=resolver)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://idp.example.com/x")

    async def test_factory_uses_the_resolver_without_an_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No override: the real path resolves and refuses before opening a socket."""

        async def resolver(host: str, port: int) -> list[str]:
            return ["169.254.169.254"]

        monkeypatch.setattr(sso_http, "_resolve_host", resolver)
        async with create_sso_client(_settings()) as client:
            with pytest.raises(SSOBlockedAddressError):
                await client.get("https://idp.example.com/x")
