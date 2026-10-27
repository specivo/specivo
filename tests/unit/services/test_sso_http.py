"""Unit tests for the SSO HTTP client factory and the SSO settings.

Every request to an identity provider goes through
``specivo.services.sso.http.create_sso_client``. These tests pin its limits:
timeout, response size cap, HTTPS enforcement, same-host redirects only, and
the transport seam the mock IdP uses.
"""

from __future__ import annotations

import gzip
import os

import httpx
import pytest

from specivo.core.config import Settings, get_settings
from specivo.services.sso import http as sso_http
from specivo.services.sso.http import (
    MAX_RESPONSE_BYTES,
    SSOHttpError,
    SSOInsecureURLError,
    SSORedirectError,
    SSOResponseTooLargeError,
    create_sso_client,
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
