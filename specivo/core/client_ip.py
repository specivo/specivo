"""The client's address, with ``X-Forwarded-For`` trusted only from trusted proxies.

The direct peer of the socket is the only address Specivo has seen for itself.
A reverse proxy in front of Specivo (the bundled nginx, Caddy, a load
balancer) appends the address *it* received the request from to
``X-Forwarded-For``. It does not append its own address: that is the peer.

So the client is found by walking the header from the right:

- If the peer is not in ``settings.trusted_proxies``, the peer is the client
  and the header is ignored. An app exposed directly cannot be fooled by a
  made-up header.
- Otherwise each hop, right to left, is skipped while it is a trusted proxy
  too; the first address that is not is the client. Anything left of it was
  written by someone Specivo does not trust.
- If every hop is trusted, the leftmost one is the client.
- A hop that is not an IP address stops the walk: the peer is returned, since
  nothing beyond it can be believed.

Every per-IP rate limit, the IP recorded on a session, the API key IP
allowlist and the security audit trail use this one function.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from typing import Any

from starlette.requests import HTTPConnection

from specivo.core import config

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
_IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=32)
def _networks(cidrs: tuple[str, ...]) -> tuple[_IPNetwork, ...]:
    """Parse ``cidrs`` once; Settings has already refused invalid ones."""
    return tuple(ipaddress.ip_network(cidr, strict=False) for cidr in cidrs)


def _parse(value: str) -> _IPAddress | None:
    """An address from a peer or an XFF hop; a port or brackets are removed."""
    value = value.strip()
    if value.startswith("["):  # [2001:db8::1]:443
        value = value[1:].partition("]")[0]
    elif value.count(":") == 1:  # 203.0.113.10:51234
        value = value.partition(":")[0]
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _trusted(addr: _IPAddress, networks: Iterable[_IPNetwork]) -> bool:
    return any(addr in net for net in networks)


def resolve_client_ip(peer: str | None, forwarded_for: str | None, trusted_proxies: Sequence[str]) -> str | None:
    """The client's address, given the socket peer and the ``X-Forwarded-For`` value."""
    if not peer:
        return None
    if not forwarded_for or not trusted_proxies:
        return peer
    networks = _networks(tuple(trusted_proxies))
    peer_addr = _parse(peer)
    if peer_addr is None or not _trusted(peer_addr, networks):
        return peer

    hops = [hop for hop in (part.strip() for part in forwarded_for.split(",")) if hop]
    if not hops:
        return peer
    leftmost: _IPAddress | None = None
    for hop in reversed(hops):
        addr = _parse(hop)
        if addr is None:
            return peer
        if not _trusted(addr, networks):
            return str(addr)
        leftmost = addr
    return str(leftmost)


def _forwarded_for(values: Iterable[str]) -> str | None:
    """Several ``X-Forwarded-For`` headers form one list, in order."""
    joined = ", ".join(v for v in values if v)
    return joined or None


def get_client_ip(request: HTTPConnection) -> str | None:
    """The client's address for ``request``; ``None`` when the peer is unknown."""
    peer = request.client.host if request.client else None
    headers: Any = request.headers
    getlist = getattr(headers, "getlist", None)
    values = getlist("x-forwarded-for") if getlist else [headers.get("X-Forwarded-For") or ""]
    return resolve_client_ip(peer, _forwarded_for(values), config.get_settings().trusted_proxies)


def client_ip_from_scope(scope: Mapping[str, Any]) -> str | None:
    """:func:`get_client_ip` for an ASGI scope, for middleware below the Request."""
    client = scope.get("client")
    peer = client[0] if client else None
    values = [
        value.decode("latin-1") for name, value in scope.get("headers") or [] if name.lower() == b"x-forwarded-for"
    ]
    return resolve_client_ip(peer, _forwarded_for(values), config.get_settings().trusted_proxies)
