"""The one HTTP client factory for calls to identity providers.

Discovery, JWKS, token and userinfo requests all use
:func:`create_sso_client`, so the limits below hold for every IdP call:

* per-phase timeouts (connect, read, write, pool) **and** a total deadline
  per request, both ``SSO_HTTP_TIMEOUT_SECONDS``. The deadline spans redirect
  hops and the body, also when the caller streams it, so a slow-drip server
  cannot hold a request open past it;
* a response body cap of :data:`MAX_RESPONSE_BYTES`, counted on the bytes
  actually received, not on ``Content-Length``; compressed bodies are not
  accepted, so the cap cannot be bypassed with a decompression bomb;
* HTTPS only, unless ``SSO_ALLOW_INSECURE_HTTP`` is on (also for each
  redirect hop);
* redirects are followed only within the same host and port, at most
  :data:`MAX_REDIRECTS` times;
* link-local and cloud metadata addresses are refused on every hop, even
  with insecure HTTP on (see :func:`is_blocked_address`). Private and
  loopback ranges stay allowed: self-hosted IdPs on internal networks are
  the main use case, and only admins configure IdP URLs. The host name is
  resolved once per hop, every answer is checked, and the connection is
  pinned to the checked addresses (TLS still verifies the host name through
  SNI), so DNS rebinding cannot swap the address after the check. The
  answers are tried in order and the next one is used when connecting fails
  (``localhost`` resolving to ``::1`` for an IPv4-only IdP, a broken IPv6
  path), all within the request's total deadline;
* proxy and certificate settings from the environment are ignored
  (``trust_env=False``).

Tests replace the network with :func:`override_transport`, which installs an
``httpx.MockTransport`` (or any async transport) for every client created
while it is active. Every limit above still applies on top of it, except
that host names are not resolved (IP literals are still checked).

All errors raised here derive from :class:`SSOHttpError`, itself an
``httpx.RequestError``, so callers can treat "IdP unreachable or misbehaving"
as one ``except httpx.HTTPError`` branch.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx

from specivo.core.config import Settings, get_settings

#: Largest response body accepted from an identity provider.
MAX_RESPONSE_BYTES = 1024 * 1024

#: Same-host redirects followed before giving up.
MAX_REDIRECTS = 3

#: Destinations never contacted: link-local ranges, which hold the cloud
#: instance metadata services (169.254.169.254 on AWS, GCP, Azure and others),
#: plus the metadata addresses outside them.
BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("169.254.0.0/16"),  # IPv4 link-local
    ipaddress.ip_network("100.100.100.200/32"),  # Alibaba Cloud metadata
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS metadata over IPv6
)

# NAT64 well-known prefix (RFC 6052): the last 32 bits embed an IPv4 address.
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")

_REQUEST_DEADLINE_KEY = "specivo_sso_deadline"

Resolver = Callable[[str, int], Awaitable[list[str]]]

_transport_override: httpx.AsyncBaseTransport | None = None


class SSOHttpError(httpx.RequestError):
    """Base class for refusals by the SSO HTTP client."""


class SSOInsecureURLError(SSOHttpError):
    """A request (or redirect hop) to a non-HTTPS URL while insecure HTTP is off."""


class SSORedirectError(SSOHttpError):
    """A redirect to another host or port."""


class SSOResponseTooLargeError(SSOHttpError):
    """The response body exceeds :data:`MAX_RESPONSE_BYTES`."""


class SSOBlockedAddressError(SSOHttpError):
    """The destination is a link-local or cloud metadata address."""


class SSODeadlineExceededError(httpx.TimeoutException, SSOHttpError):
    """The whole request, body included, took longer than the SSO timeout."""


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
# Destination checks
# ---------------------------------------------------------------------------


def is_blocked_address(address: str) -> bool:
    """Whether an IP address is a destination the SSO client never contacts.

    IPv4-mapped (``::ffff:a.b.c.d``) and NAT64 (``64:ff9b::/96``) forms are
    checked as the IPv4 address they carry. A zone suffix (``%eth0``) is
    ignored. Raises ``ValueError`` for a string that is not an IP address.
    """
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            candidates.append(ip.ipv4_mapped)
        if ip in _NAT64_PREFIX:
            candidates.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return any(candidate in net for candidate in candidates for net in BLOCKED_NETWORKS)


def _ip_literal(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


async def _resolve_host(host: str, port: int) -> list[str]:
    """All addresses ``host`` resolves to (non-blocking ``getaddrinfo``)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def _default_resolver(host: str, port: int) -> list[str]:
    # Looked up at call time so tests can replace ``_resolve_host``.
    return await _resolve_host(host, port)


class _GuardedTransport(httpx.AsyncBaseTransport):
    """Refuses blocked destinations and pins each request to a checked address.

    With ``resolver=None`` (the test seam) only IP-literal hosts are checked
    and requests pass through unchanged. With a resolver, the host is resolved,
    every answer must pass :func:`is_blocked_address`, and the request is sent
    to the answers in order, with the original ``Host`` header and TLS server
    name, moving to the next one only when connecting fails.
    The transport is called once per redirect hop, so every hop is checked.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, *, resolver: Resolver | None) -> None:
        self._inner = inner
        self._resolver = resolver

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        literal = _ip_literal(host)
        if literal is not None:
            self._refuse_if_blocked([literal], request)
            return await self._inner.handle_async_request(request)
        if self._resolver is None:
            return await self._inner.handle_async_request(request)

        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        try:
            addresses = await self._resolver(host, port)
        except OSError as exc:
            raise httpx.ConnectError(f"Could not resolve identity provider host {host!r}", request=request) from exc
        if not addresses:
            raise httpx.ConnectError(f"Identity provider host {host!r} has no address", request=request)
        self._refuse_if_blocked(addresses, request)

        extensions: dict[str, Any] = dict(request.extensions)
        if request.url.scheme == "https":
            extensions["sni_hostname"] = host
        # Every answer passed the check above; try them in order. A dual-stack
        # name (``localhost`` answers ::1 first) or a broken IPv6 path must not
        # fail when the next address works. Only a failure to *connect* moves
        # on: after any byte may have been sent the error is raised as is. The
        # client's total deadline still bounds all attempts together.
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
                return await self._inner.handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
        if last_error is None:  # pragma: no cover - addresses is never empty here
            raise httpx.ConnectError(f"Identity provider host {host!r} has no address", request=request)
        raise last_error

    @staticmethod
    def _refuse_if_blocked(addresses: list[str], request: httpx.Request) -> None:
        if any(is_blocked_address(address) for address in addresses):
            raise SSOBlockedAddressError(
                "Refusing a link-local or cloud metadata identity provider address", request=request
            )

    async def aclose(self) -> None:
        await self._inner.aclose()


# ---------------------------------------------------------------------------
# Size cap and streaming deadline
# ---------------------------------------------------------------------------


class _CappedStream(httpx.AsyncByteStream):
    """Yields the inner stream's chunks until the size cap or the deadline is hit."""

    def __init__(
        self, inner: httpx.AsyncByteStream, limit: int, request: httpx.Request, deadline: float | None
    ) -> None:
        self._inner = inner
        self._limit = limit
        self._request = request
        self._deadline = deadline

    async def __aiter__(self) -> AsyncIterator[bytes]:
        received = 0
        chunks = self._inner.__aiter__()
        while True:
            try:
                async with asyncio.timeout_at(self._deadline):
                    chunk = await chunks.__anext__()
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                raise SSODeadlineExceededError(
                    "Identity provider response exceeded the SSO timeout", request=self._request
                ) from exc
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
        deadline = request.extensions.get(_REQUEST_DEADLINE_KEY)
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_CappedStream(stream, self._limit, request, deadline),
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


# ---------------------------------------------------------------------------
# Client and factory
# ---------------------------------------------------------------------------


class _SSOClient(httpx.AsyncClient):
    """An ``AsyncClient`` with a total deadline per request.

    ``httpx.Timeout`` limits each phase (connect, every read), so a server
    that drips one byte per read can hold a request open indefinitely. The
    deadline is fixed when ``send`` starts and covers redirect hops and the
    body; for a streamed response the body stream enforces the same deadline.
    """

    def __init__(self, *, deadline_seconds: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._deadline_seconds = deadline_seconds

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        deadline = asyncio.get_running_loop().time() + self._deadline_seconds
        request.extensions[_REQUEST_DEADLINE_KEY] = deadline
        try:
            async with asyncio.timeout_at(deadline):
                return await super().send(request, **kwargs)
        except TimeoutError as exc:
            raise SSODeadlineExceededError(
                "Identity provider request exceeded the SSO timeout", request=request
            ) from exc


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

    if _transport_override is not None:
        guarded = _GuardedTransport(_transport_override, resolver=None)
    else:
        guarded = _GuardedTransport(httpx.AsyncHTTPTransport(retries=0), resolver=_default_resolver)
    return _SSOClient(
        deadline_seconds=settings.sso_http_timeout_seconds,
        transport=_CappedTransport(guarded, MAX_RESPONSE_BYTES),
        timeout=httpx.Timeout(settings.sso_http_timeout_seconds),
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        trust_env=False,
        headers={"Accept-Encoding": "identity", "User-Agent": f"Specivo/{settings.version}"},
        event_hooks={"request": [_check_scheme], "response": [_check_redirect]},
    )
