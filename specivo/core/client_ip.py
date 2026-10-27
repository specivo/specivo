"""The client's address, with ``X-Forwarded-For`` believed only from a trusted proxy.

The direct peer of the socket is the only address Specivo has seen for itself.
A reverse proxy in front of Specivo (the bundled nginx, Caddy, Traefik)
appends the address *it* received the request from to ``X-Forwarded-For``; it
does not append its own address, which is the peer.

Two settings decide what is believed:

- ``trusted_proxies``: the addresses (CIDRs) of the proxy that connects to
  Specivo. Only a peer in this list may name the client. Empty (the default):
  the peer is the client and the header is ignored.
- ``trusted_proxy_hops``: how many proxies in a row stand in front of
  Specivo, each appending one entry. The client is the entry that many places
  from the right; everything left of it was written by the client or someone
  else Specivo does not trust. Default 1: the address the proxy saw.

Addresses in the header are never trusted because of the range they are in:
a client whose traffic is NAT'd into a private range, or another container on
the network, gains nothing by adding private addresses to the header.

If the header has fewer entries than ``trusted_proxy_hops``, the leftmost
one is taken: the request skipped the outer proxies, and the first proxy that
saw it wrote that entry. If the chosen entry is not an IP address, the peer
is returned.

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
    """An address from a peer or a header entry; a port or brackets are removed."""
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


def resolve_client_ip(
    peer: str | None,
    forwarded_for: str | None,
    trusted_proxies: Sequence[str],
    hops: int = 1,
) -> str | None:
    """The client's address, given the socket peer and the ``X-Forwarded-For`` value."""
    if not peer:
        return None
    if not forwarded_for or not trusted_proxies:
        return peer
    peer_addr = _parse(peer)
    if peer_addr is None or not any(peer_addr in net for net in _networks(tuple(trusted_proxies))):
        return peer

    entries = [entry for entry in (part.strip() for part in forwarded_for.split(",")) if entry]
    if not entries:
        return peer
    chosen = _parse(entries[max(0, len(entries) - max(1, hops))])
    return str(chosen) if chosen is not None else peer


def _forwarded_for(values: Iterable[str]) -> str | None:
    """Several ``X-Forwarded-For`` headers form one list, in order."""
    joined = ", ".join(v for v in values if v)
    return joined or None


def _resolve(peer: str | None, values: Iterable[str]) -> str | None:
    settings = config.get_settings()
    hops = settings.trusted_proxy_hops if isinstance(settings.trusted_proxy_hops, int) else 1
    return resolve_client_ip(peer, _forwarded_for(values), settings.trusted_proxies, hops)


def get_client_ip(request: HTTPConnection) -> str | None:
    """The client's address for ``request``; ``None`` when the peer is unknown."""
    peer = request.client.host if request.client else None
    headers: Any = request.headers
    getlist = getattr(headers, "getlist", None)
    values = getlist("x-forwarded-for") if getlist else [headers.get("X-Forwarded-For") or ""]
    return _resolve(peer, values)


def client_ip_from_scope(scope: Mapping[str, Any]) -> str | None:
    """:func:`get_client_ip` for an ASGI scope, for middleware below the Request."""
    client = scope.get("client")
    peer = client[0] if client else None
    values = [
        value.decode("latin-1") for name, value in scope.get("headers") or [] if name.lower() == b"x-forwarded-for"
    ]
    return _resolve(peer, values)
