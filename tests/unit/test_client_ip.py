"""Which address is the client: the trust rules for ``X-Forwarded-For``.

The direct peer is the only address Specivo has seen for itself. A proxy in
front of it (the bundled nginx, Caddy, a load balancer) appends the address
*it* saw to ``X-Forwarded-For``; it does not append its own. So the client is
found by walking the header from the right, past every hop that is a trusted
proxy, and taking the first address that is not. Everything left of that
address was written by someone Specivo does not trust and is ignored.

A peer that is not a trusted proxy is the client, whatever header it sends.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from starlette.datastructures import Headers

from specivo.core.client_ip import client_ip_from_scope, get_client_ip, resolve_client_ip
from specivo.core.config import get_settings

pytestmark = pytest.mark.unit

# The bundled compose default: Docker's private ranges.
COMPOSE = ["172.16.0.0/12", "10.0.0.0/8"]
NGINX = "172.18.0.5"


class TestBehindTheBundledNginx:
    def test_client_is_the_address_nginx_appended(self) -> None:
        """nginx's ``$proxy_add_x_forwarded_for`` adds the client, not nginx itself."""
        assert resolve_client_ip(NGINX, "203.0.113.10", COMPOSE) == "203.0.113.10"

    def test_spoofed_header_from_the_client_is_ignored(self) -> None:
        """The client sent its own XFF; nginx appended the real address after it."""
        assert resolve_client_ip(NGINX, "1.2.3.4, 203.0.113.10", COMPOSE) == "203.0.113.10"

    def test_spoofed_trusted_hop_does_not_shift_the_answer(self) -> None:
        """A client claiming to be behind an internal proxy is still itself."""
        assert resolve_client_ip(NGINX, "1.2.3.4, 10.9.9.9, 203.0.113.10", COMPOSE) == "203.0.113.10"

    def test_https_terminator_on_the_host_in_front_of_nginx(self) -> None:
        """Caddy on the host: its address (the Docker gateway) is trusted and skipped."""
        assert resolve_client_ip(NGINX, "203.0.113.10, 172.18.0.1", COMPOSE) == "203.0.113.10"

    def test_internal_client_when_every_hop_is_trusted(self) -> None:
        """Every hop trusted: the leftmost address is the client."""
        assert resolve_client_ip(NGINX, "10.1.2.3", COMPOSE) == "10.1.2.3"

    def test_no_header_means_the_peer(self) -> None:
        assert resolve_client_ip(NGINX, None, COMPOSE) == NGINX
        assert resolve_client_ip(NGINX, "", COMPOSE) == NGINX


class TestUntrustedPeer:
    def test_nothing_trusted_means_the_peer(self) -> None:
        assert resolve_client_ip("203.0.113.99", "1.2.3.4", []) == "203.0.113.99"

    def test_peer_outside_trusted_ranges_cannot_spoof(self) -> None:
        """Exposed directly: a client's XFF never replaces its own address."""
        assert resolve_client_ip("203.0.113.99", "1.2.3.4", COMPOSE) == "203.0.113.99"

    def test_peer_listing_itself_in_the_header_gains_nothing(self) -> None:
        assert resolve_client_ip("203.0.113.99", "1.2.3.4, 203.0.113.99", COMPOSE) == "203.0.113.99"

    def test_unparseable_peer_is_returned_as_is(self) -> None:
        assert resolve_client_ip("testclient", "1.2.3.4", COMPOSE) == "testclient"

    def test_no_peer(self) -> None:
        assert resolve_client_ip(None, "1.2.3.4", COMPOSE) is None


class TestSeveralProxies:
    def test_cdn_then_nginx(self) -> None:
        """A CDN range is trusted too: walk past it to the client."""
        trusted = [*COMPOSE, "198.51.100.0/24"]
        assert resolve_client_ip(NGINX, "1.2.3.4, 203.0.113.10, 198.51.100.7", trusted) == "203.0.113.10"

    def test_cdn_not_trusted_stops_at_the_cdn(self) -> None:
        """Without the CDN range, the CDN edge is the client as far as Specivo can tell."""
        assert resolve_client_ip(NGINX, "203.0.113.10, 198.51.100.7", COMPOSE) == "198.51.100.7"

    def test_several_headers_are_one_list(self) -> None:
        assert resolve_client_ip(NGINX, "203.0.113.10 , 172.18.0.1", COMPOSE) == "203.0.113.10"

    def test_malformed_hop_stops_the_walk_at_the_peer(self) -> None:
        """Garbage where an address should be: nothing beyond the peer is believed."""
        assert resolve_client_ip(NGINX, "203.0.113.10, not-an-ip", COMPOSE) == NGINX

    def test_hop_with_a_port(self) -> None:
        assert resolve_client_ip(NGINX, "203.0.113.10:51234", COMPOSE) == "203.0.113.10"


class TestIPv6:
    def test_ipv6_proxy_and_client(self) -> None:
        trusted = ["fd00::/8"]
        assert resolve_client_ip("fd00::5", "2001:db8::1", trusted) == "2001:db8::1"

    def test_bracketed_ipv6_with_port(self) -> None:
        trusted = ["fd00::/8"]
        assert resolve_client_ip("fd00::5", "[2001:db8::1]:443", trusted) == "2001:db8::1"

    def test_ipv4_mapped_peer_matches_an_ipv4_range(self) -> None:
        """A dual-stack socket reports nginx as ``::ffff:172.18.0.5``."""
        assert resolve_client_ip("::ffff:172.18.0.5", "203.0.113.10", COMPOSE) == "203.0.113.10"

    def test_untrusted_ipv6_peer(self) -> None:
        assert resolve_client_ip("2001:db8::99", "1.2.3.4", ["fd00::/8"]) == "2001:db8::99"


class TestRequestAndScope:
    def test_request_uses_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(get_settings(), "trusted_proxies", COMPOSE)
        request = MagicMock()
        request.client.host = NGINX
        request.headers = Headers(raw=[(b"x-forwarded-for", b"1.2.3.4"), (b"x-forwarded-for", b"203.0.113.10")])
        assert get_client_ip(request) == "203.0.113.10"

    def test_request_without_client(self) -> None:
        request = MagicMock()
        request.client = None
        request.headers = Headers()
        assert get_client_ip(request) is None

    def test_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(get_settings(), "trusted_proxies", COMPOSE)
        scope = {"client": (NGINX, 4321), "headers": [(b"x-forwarded-for", b"203.0.113.10")]}
        assert client_ip_from_scope(scope) == "203.0.113.10"

    def test_scope_untrusted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(get_settings(), "trusted_proxies", [])
        scope = {"client": (NGINX, 4321), "headers": [(b"x-forwarded-for", b"203.0.113.10")]}
        assert client_ip_from_scope(scope) == NGINX

    def test_scope_without_client(self) -> None:
        assert client_ip_from_scope({"headers": []}) is None


class TestSettings:
    def test_invalid_cidr_is_refused_at_startup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        from specivo.core.config import Settings

        monkeypatch.setenv("TRUSTED_PROXIES", '["not-a-network"]')
        with pytest.raises(ValidationError, match="TRUSTED_PROXIES"):
            Settings()

    def test_json_list_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from specivo.core.config import Settings

        monkeypatch.setenv("TRUSTED_PROXIES", '["172.16.0.0/12", "10.0.0.0/8"]')
        assert Settings().trusted_proxies == ["172.16.0.0/12", "10.0.0.0/8"]
