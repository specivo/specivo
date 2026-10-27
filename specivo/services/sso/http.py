"""The one HTTP client factory for calls to identity providers.

Discovery, JWKS, token and userinfo requests all use
:func:`create_sso_client`, so the limits below hold for every IdP call:

* a per-request timeout (``SSO_HTTP_TIMEOUT_SECONDS``);
* a response body cap of :data:`MAX_RESPONSE_BYTES`, counted on the bytes
  actually received, not on ``Content-Length``; compressed bodies are not
  accepted, so the cap cannot be bypassed with a decompression bomb;
* HTTPS only, unless ``SSO_ALLOW_INSECURE_HTTP`` is on (also for each
  redirect hop);
* redirects are followed only within the same host and port, at most
  :data:`MAX_REDIRECTS` times;
* proxy and certificate settings from the environment are ignored
  (``trust_env=False``).

Tests replace the network with :func:`override_transport`, which installs an
``httpx.MockTransport`` (or any async transport) for every client created
while it is active. The limits above still apply on top of it.

All errors raised here derive from :class:`SSOHttpError`, itself an
``httpx.RequestError``, so callers can treat "IdP unreachable or misbehaving"
as one ``except httpx.HTTPError`` branch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

import httpx

from specivo.core.config import Settings, get_settings

#: Largest response body accepted from an identity provider.
MAX_RESPONSE_BYTES = 1024 * 1024

#: Same-host redirects followed before giving up.
MAX_REDIRECTS = 3

_transport_override: httpx.AsyncBaseTransport | None = None


class SSOHttpError(httpx.RequestError):
    """Base class for refusals by the SSO HTTP client."""


class SSOInsecureURLError(SSOHttpError):
    """A request (or redirect hop) to a non-HTTPS URL while insecure HTTP is off."""


class SSORedirectError(SSOHttpError):
    """A redirect to another host or port."""


class SSOResponseTooLargeError(SSOHttpError):
    """The response body exceeds :data:`MAX_RESPONSE_BYTES`."""


# ---------------------------------------------------------------------------
# Test seam
# ---------------------------------------------------------------------------


def get_transport_override() -> httpx.AsyncBaseTransport | None:
    """The transport installed by :func:`override_transport`, if any."""
    return _transport_override


@contextmanager
def override_transport(transport: httpx.AsyncBaseTransport) -> Iterator[httpx.AsyncBaseTransport]:
    """Route every SSO client created inside the block through ``transport``.

    Meant for tests (``httpx.MockTransport``). Overrides nest; the previous
    one is restored on exit, also when the block raises.
    """
    global _transport_override
    previous = _transport_override
    _transport_override = transport
    try:
        yield transport
    finally:
        _transport_override = previous


# ---------------------------------------------------------------------------
# Size cap
# ---------------------------------------------------------------------------


class _CappedStream(httpx.AsyncByteStream):
    """Yields the inner stream's chunks until more than ``limit`` bytes arrived."""

    def __init__(self, inner: httpx.AsyncByteStream, limit: int, request: httpx.Request) -> None:
        self._inner = inner
        self._limit = limit
        self._request = request

    async def __aiter__(self) -> AsyncIterator[bytes]:
        received = 0
        async for chunk in self._inner:
            received += len(chunk)
            if received > self._limit:
                raise SSOResponseTooLargeError(
                    f"Identity provider response exceeds {self._limit} bytes", request=self._request
                )
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class _CappedTransport(httpx.AsyncBaseTransport):
    """Wraps a transport and enforces the response size cap and identity encoding."""

    def __init__(self, inner: httpx.AsyncBaseTransport, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)

        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            await response.aclose()
            raise SSOHttpError(f"Identity provider sent a {encoding!r}-encoded response", request=request)

        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self._limit:
            await response.aclose()
            raise SSOResponseTooLargeError(f"Identity provider response exceeds {self._limit} bytes", request=request)

        stream = response.stream
        if not isinstance(stream, httpx.AsyncByteStream):  # pragma: no cover - sync-only stream
            raise SSOHttpError("Transport returned a synchronous stream", request=request)
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_CappedStream(stream, self._limit, request),
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_sso_client(settings: Settings | None = None) -> httpx.AsyncClient:
    """Return a new ``httpx.AsyncClient`` for identity provider calls.

    Use it as an async context manager so the connection pool is closed::

        async with create_sso_client() as client:
            resp = await client.get(jwks_uri)
    """
    settings = settings or get_settings()
    allow_insecure = settings.sso_allow_insecure_http

    async def _check_scheme(request: httpx.Request) -> None:
        scheme = request.url.scheme
        if scheme == "https" or (allow_insecure and scheme == "http"):
            return
        raise SSOInsecureURLError(f"Refusing non-HTTPS identity provider URL ({scheme}://)", request=request)

    async def _check_redirect(response: httpx.Response) -> None:
        if not response.has_redirect_location:
            return
        source = response.request.url
        target = source.join(response.headers["Location"])
        if (target.host, target.port) != (source.host, source.port):
            raise SSORedirectError("Refusing a redirect to another host", request=response.request)

    inner = _transport_override or httpx.AsyncHTTPTransport(retries=0)
    return httpx.AsyncClient(
        transport=_CappedTransport(inner, MAX_RESPONSE_BYTES),
        timeout=httpx.Timeout(settings.sso_http_timeout_seconds),
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        trust_env=False,
        headers={"Accept-Encoding": "identity", "User-Agent": f"Specivo/{settings.version}"},
        event_hooks={"request": [_check_scheme], "response": [_check_redirect]},
    )
