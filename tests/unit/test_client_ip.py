"""Which address is the client: the trust rules for ``X-Forwarded-For``.

The direct peer is the only address Specivo has seen for itself. A proxy in
front of it appends the address *it* saw to ``X-Forwarded-For``. Only a peer
listed in ``TRUSTED_PROXIES`` may name the client, and then the client is the
entry ``TRUSTED_PROXY_HOPS`` places from the right. Entries are never believed
because of the address range they fall in.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from starlette.datastructures import Headers

from specivo.core.client_ip import client_ip_from_scope, get_client_ip, resolve_client_ip
from specivo.core.config import get_settings

pytestmark = pytest.mark.unit

# The bundled compose default: exactly the nginx container on its own network.
NGINX = "172.31.253.2"
TRUSTED = [f"{NGINX}/32"]


class TestBehindTheBundledNginx:
    def test_client_is_the_address_nginx_appended(self) -> None:
        """nginx's ``$proxy_add_x_forwarded_for`` adds the client, not nginx itself."""
        assert resolve_client_ip(NGINX, "203.0.113.10", TRUSTED) == "203.0.113.10"

    def test_spoofed_header_from_the_client_is_ignored(self) -> None:
        """The client sent its own XFF; nginx appended the real address after it."""
        assert resolve_client_ip(NGINX, "1.2.3.4, 203.0.113.10", TRUSTED) == "203.0.113.10"

    def test_forged_private_hops_on_the_left_gain_nothing(self) -> None:
        """Forged entries that look like internal proxies are not skipped."""
        assert resolve_client_ip(NGINX, "1.2.3.4, 10.9.9.9, 172.18.0.1, 203.0.113.10", TRUSTED) == "203.0.113.10"

    def test_client_natted_into_a_private_range_cannot_spoof(self) -> None:
        """A client reaching nginx as 10.0.0.7 is 10.0.0.7, whatever it puts left of that."""
        assert resolve_client_ip(NGINX, "1.2.3.4, 10.0.0.7", TRUSTED) == "10.0.0.7"
        # Even when someone (wrongly) trusts the whole private range.
        assert resolve_client_ip(NGINX, "1.2.3.4, 10.0.0.7", [f"{NGINX}/32", "10.0.0.0/8"]) == "10.0.0.7"

    def test_no_header_means_the_peer(self) -> None:
        assert resolve_client_ip(NGINX, None, TRUSTED) == NGINX
        assert resolve_client_ip(NGINX, "", TRUSTED) == NGINX
        assert resolve_client_ip(NGINX, " , ", TRUSTED) == NGINX

    def test_malformed_entry_means_the_peer(self) -> None:
        assert resolve_client_ip(NGINX, "203.0.113.10, not-an-ip", TRUSTED) == NGINX

    def test_entry_with_a_port(self) -> None:
        assert resolve_client_ip(NGINX, "203.0.113.10:51234", TRUSTED) == "203.0.113.10"


class TestUntrustedPeer:
    def test_nothing_trusted_means_the_peer(self) -> None:
        assert resolve_client_ip("203.0.113.99", "1.2.3.4", []) == "203.0.113.99"

    def test_peer_outside_trusted_proxies_cannot_spoof(self) -> None:
        """Exposed directly: a client's XFF never replaces its own address."""
        assert resolve_client_ip("203.0.113.99", "1.2.3.4", TRUSTED) == "203.0.113.99"

    def test_another_container_on_the_network_cannot_spoof(self) -> None:
        """A neighbour container (or the Docker gateway) is not the trusted nginx."""
        assert resolve_client_ip("172.31.253.3", "1.2.3.4", TRUSTED) == "172.31.253.3"
        assert resolve_client_ip("172.31.253.1", "1.2.3.4", TRUSTED) == "172.31.253.1"

    def test_peer_listing_itself_in_the_header_gains_nothing(self) -> None:
        assert resolve_client_ip("203.0.113.99", "1.2.3.4, 203.0.113.99", TRUSTED) == "203.0.113.99"

    def test_unparseable_peer_is_returned_as_is(self) -> None:
        assert resolve_client_ip("testclient", "1.2.3.4", TRUSTED) == "testclient"

    def test_no_peer(self) -> None:
        assert resolve_client_ip(None, "1.2.3.4", TRUSTED) is None


class TestSeveralProxies:
    def test_terminator_in_front_of_nginx_needs_two_hops(self) -> None:
        """Caddy, then nginx: Caddy wrote the client, nginx appended Caddy's address."""
        xff = "1.2.3.4, 203.0.113.10, 172.31.253.1"
        assert resolve_client_ip(NGINX, xff, TRUSTED, hops=2) == "203.0.113.10"
        # With one hop only, the terminator is taken for the client: safe, not spoofable.
        assert resolve_client_ip(NGINX, xff, TRUSTED, hops=1) == "172.31.253.1"

    def test_fewer_entries_than_hops_takes_the_leftmost(self) -> None:
        """The request skipped the outer proxy; the first proxy that saw it wrote the entry."""
        assert resolve_client_ip(NGINX, "203.0.113.10", TRUSTED, hops=2) == "203.0.113.10"

    def test_hops_below_one_counts_as_one(self) -> None:
        assert resolve_client_ip(NGINX, "1.2.3.4, 203.0.113.10", TRUSTED, hops=0) == "203.0.113.10"


class TestIPv6:
    def test_ipv6_proxy_and_client(self) -> None:
        assert resolve_client_ip("fd00::5", "2001:db8::1", ["fd00::5/128"]) == "2001:db8::1"

    def test_ipv6_forged_left_entries(self) -> None:
        assert resolve_client_ip("fd00::5", "2001:db8::bad, fd00::9, 2001:db8::1", ["fd00::/8"]) == "2001:db8::1"

    def test_bracketed_ipv6_with_port(self) -> None:
        assert resolve_client_ip("fd00::5", "[2001:db8::1]:443", ["fd00::/8"]) == "2001:db8::1"

    def test_ipv4_mapped_peer_matches_an_ipv4_entry(self) -> None:
        """A dual-stack socket reports nginx as ``::ffff:172.31.253.2``."""
        assert resolve_client_ip(f"::ffff:{NGINX}", "203.0.113.10", TRUSTED) == "203.0.113.10"

    def test_untrusted_ipv6_peer(self) -> None:
        assert resolve_client_ip("2001:db8::99", "1.2.3.4", ["fd00::/8"]) == "2001:db8::99"


class TestRequestAndScope:
    def test_request_uses_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(get_settings(), "trusted_proxies", TRUSTED)
        request = MagicMock()
        request.client.host = NGINX
        request.headers = Headers(raw=[(b"x-forwarded-for", b"1.2.3.4"), (b"x-forwarded-for", b"203.0.113.10")])
        assert get_client_ip(request) == "203.0.113.10"

    def test_request_uses_the_hop_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(get_settings(), "trusted_proxies", TRUSTED)
        monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 2)
        request = MagicMock()
        request.client.host = NGINX
        request.headers = Headers({"x-forwarded-for": "1.2.3.4, 203.0.113.10, 172.31.253.1"})
        assert get_client_ip(request) == "203.0.113.10"

    def test_request_without_client(self) -> None:
        request = MagicMock()
        request.client = None
        request.headers = Headers()
        assert get_client_ip(request) is None

    def test_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(get_settings(), "trusted_proxies", TRUSTED)
        scope = {"client": (NGINX, 4321), "headers": [(b"x-forwarded-for", b"203.0.113.10")]}
        assert client_ip_from_scope(scope) == "203.0.113.10"

    def test_scope_untrusted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(get_settings(), "trusted_proxies", [])
        scope = {"client": (NGINX, 4321), "headers": [(b"x-forwarded-for", b"203.0.113.10")]}
        assert client_ip_from_scope(scope) == NGINX

    def test_scope_without_client(self) -> None:
        assert client_ip_from_scope({"headers": []}) is None


class TestSettings:
    def test_nothing_is_trusted_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from specivo.core.config import Settings

        monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
        monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
        settings = Settings()
        assert settings.trusted_proxies == []
        assert settings.trusted_proxy_hops == 1

    def test_invalid_cidr_is_refused_at_startup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        from specivo.core.config import Settings

        monkeypatch.setenv("TRUSTED_PROXIES", '["not-a-network"]')
        with pytest.raises(ValidationError, match="TRUSTED_PROXIES"):
            Settings()

    def test_hops_must_be_positive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        from specivo.core.config import Settings

        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "0")
        with pytest.raises(ValidationError):
            Settings()

    def test_json_list_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from specivo.core.config import Settings

        monkeypatch.setenv("TRUSTED_PROXIES", '["172.31.253.2/32", "fd00::/8"]')
        assert Settings().trusted_proxies == ["172.31.253.2/32", "fd00::/8"]
