"""Server-side request forgery guards for outgoing HTTP calls to user-supplied URLs.

A URL entered by a user (an outgoing webhook) must reach the public internet
only. :func:`is_public_address` decides that for one address; anything the
``ipaddress`` module does not call globally routable is refused, which covers
loopback, private, link-local, carrier-grade NAT, documentation, multicast and
reserved ranges, and IPv4 addresses wrapped in IPv6.

Checking the host when the URL is saved is not enough: its DNS records can
change afterwards (DNS rebinding). :func:`resolve_public_addresses` resolves
the host right before a request and refuses it when any address is not
public; the caller then connects to an address it returned, not to the name.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: Resolves ``(host, port)`` to addresses; injectable for tests.
Resolver = Callable[[str, int], Awaitable[list[str]]]


class BlockedAddressError(ValueError):
    """The host is, or resolves to, an address outgoing requests may not reach."""


class UnresolvableHostError(OSError):
    """The host name did not resolve; may be temporary."""


def is_public_address(ip: IPAddress) -> bool:
    """Whether *ip* is a globally routable unicast address."""
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or ip.sixtofour
        if mapped is not None:
            return is_public_address(mapped)
    return ip.is_global and not ip.is_multicast


def is_blocked_literal(host: str) -> bool:
    """True when *host* is an IP literal that is not public; host names return False."""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return not is_public_address(ip)


async def system_resolver(host: str, port: int) -> list[str]:
    """Resolve *host* with the system resolver, without blocking the event loop."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def resolve_public_addresses(host: str, port: int, resolver: Resolver | None = None) -> list[IPAddress]:
    """Resolve *host* and return its addresses, all of them public.

    Raises :class:`UnresolvableHostError` when the host does not resolve and
    :class:`BlockedAddressError` when any of its addresses is not public: one
    bad record is enough to refuse, so a resolver cannot be steered into
    picking it.
    """
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        try:
            raw = await (resolver or system_resolver)(host, port)
        except (OSError, UnicodeError, ValueError) as exc:
            # UnicodeError: a label too long for IDNA, raised before any lookup.
            raise UnresolvableHostError(f"cannot resolve {host!r}") from exc
        addresses = [ipaddress.ip_address(a.split("%", 1)[0]) for a in raw]
    else:
        addresses = [literal]
    if not addresses:
        raise UnresolvableHostError(f"cannot resolve {host!r}")
    for ip in addresses:
        if not is_public_address(ip):
            raise BlockedAddressError(f"{host!r} resolves to a non-public address")
    # Keep order, drop duplicates (one address per socket type and protocol).
    return list(dict.fromkeys(addresses))
