"""Unit tests for OpenID provider discovery (``specivo.services.sso.discovery``).

Everything runs against the mock IdP; Redis is the in-memory ``fake_redis``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from specivo.core.config import get_settings
from specivo.services.sso import discovery
from specivo.services.sso.discovery import (
    DISCOVERY_CACHE_TTL_SECONDS,
    ProviderMetadata,
    discovery_cache_key,
    get_provider_metadata,
    invalidate_provider_cache,
    parse_discovery,
)
from specivo.services.sso.errors import SsoDiscoveryInvalidError, SsoIdpUnreachableError
from specivo.testing.oidc_mock import OMIT, MockIdP, override_transport
from tests.unit.services.conftest import BrokenRedis, FakeRedis, make_provider

pytestmark = pytest.mark.unit


def _settings(**overrides: Any) -> Any:
    return get_settings().model_copy(update=overrides)


# ---------------------------------------------------------------------------
# parse_discovery (pure)
# ---------------------------------------------------------------------------


class TestParseDiscovery:
    def test_valid_document(self, mock_idp: MockIdP) -> None:
        meta = parse_discovery(mock_idp.discovery_document(), expected_issuer=mock_idp.issuer)
        assert isinstance(meta, ProviderMetadata)
        assert meta.issuer == mock_idp.issuer
        assert meta.token_endpoint == mock_idp.token_endpoint
        assert meta.jwks_uri == mock_idp.jwks_uri
        assert meta.userinfo_endpoint == mock_idp.userinfo_endpoint
        assert meta.end_session_endpoint == mock_idp.end_session_endpoint
        assert meta.id_token_signing_alg_values_supported == ("RS256", "ES256")
        assert meta.authorization_response_iss_parameter_supported is True
        assert meta.backchannel_logout_supported is True

    def test_issuer_mismatch_is_refused(self, mock_idp: MockIdP) -> None:
        doc = mock_idp.discovery_document()
        with pytest.raises(SsoDiscoveryInvalidError) as exc:
            parse_discovery(doc, expected_issuer=mock_idp.issuer + "/")
        assert exc.value.code == "sso_discovery_invalid"
        assert exc.value.reason == "issuer_mismatch"

    def test_issuer_comparison_is_exact_not_case_folded(self, mock_idp: MockIdP) -> None:
        with pytest.raises(SsoDiscoveryInvalidError):
            parse_discovery(mock_idp.discovery_document(), expected_issuer=mock_idp.issuer.upper())

    @pytest.mark.parametrize("field", ["issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"])
    def test_required_field_missing(self, mock_idp: MockIdP, field: str) -> None:
        mock_idp.discovery_overrides[field] = OMIT
        with pytest.raises(SsoDiscoveryInvalidError) as exc:
            parse_discovery(mock_idp.discovery_document(), expected_issuer=mock_idp.issuer)
        assert field in exc.value.reason or exc.value.reason == "issuer_mismatch"

    def test_code_response_type_required(self, mock_idp: MockIdP) -> None:
        mock_idp.discovery_overrides["response_types_supported"] = ["id_token"]
        with pytest.raises(SsoDiscoveryInvalidError) as exc:
            parse_discovery(mock_idp.discovery_document(), expected_issuer=mock_idp.issuer)
        assert exc.value.reason == "code_flow_unsupported"

    @pytest.mark.parametrize(
        "field", ["authorization_endpoint", "token_endpoint", "jwks_uri", "userinfo_endpoint", "end_session_endpoint"]
    )
    def test_http_endpoint_refused(self, mock_idp: MockIdP, field: str) -> None:
        mock_idp.discovery_overrides[field] = "http://idp.example.com/endpoint"
        with pytest.raises(SsoDiscoveryInvalidError) as exc:
            parse_discovery(mock_idp.discovery_document(), expected_issuer=mock_idp.issuer)
        assert exc.value.reason == f"insecure_{field}"

    def test_http_endpoint_allowed_with_insecure_flag(self, mock_idp: MockIdP) -> None:
        mock_idp.discovery_overrides["jwks_uri"] = "http://idp.example.com/certs"
        meta = parse_discovery(mock_idp.discovery_document(), expected_issuer=mock_idp.issuer, allow_insecure_http=True)
        assert meta.jwks_uri == "http://idp.example.com/certs"

    def test_http_issuer_refused_without_flag(self) -> None:
        idp = MockIdP(issuer="http://idp.example.com/realms/test")
        with pytest.raises(SsoDiscoveryInvalidError) as exc:
            parse_discovery(idp.discovery_document(), expected_issuer=idp.issuer)
        assert exc.value.reason == "insecure_issuer"

    @pytest.mark.parametrize(
        "bad", ["not a url", "ftp://idp.example.com/x", "https:///no-host", "https://[::1/", "https://xn--/", 42, None]
    )
    def test_malformed_endpoint(self, mock_idp: MockIdP, bad: Any) -> None:
        mock_idp.discovery_overrides["token_endpoint"] = bad
        with pytest.raises(SsoDiscoveryInvalidError):
            parse_discovery(mock_idp.discovery_document(), expected_issuer=mock_idp.issuer)

    def test_optional_endpoint_wrong_type(self, mock_idp: MockIdP) -> None:
        mock_idp.discovery_overrides["userinfo_endpoint"] = ["https://idp.example.com/u"]
        with pytest.raises(SsoDiscoveryInvalidError):
            parse_discovery(mock_idp.discovery_document(), expected_issuer=mock_idp.issuer)

    def test_url_with_fragment_refused(self, mock_idp: MockIdP) -> None:
        mock_idp.discovery_overrides["token_endpoint"] = "https://idp.example.com/token#frag"
        with pytest.raises(SsoDiscoveryInvalidError):
            parse_discovery(mock_idp.discovery_document(), expected_issuer=mock_idp.issuer)

    def test_not_an_object(self, mock_idp: MockIdP) -> None:
        with pytest.raises(SsoDiscoveryInvalidError) as exc:
            parse_discovery(["issuer"], expected_issuer=mock_idp.issuer)  # type: ignore[arg-type]
        assert exc.value.reason == "not_an_object"

    def test_only_unsafe_algorithms_refused(self, mock_idp: MockIdP) -> None:
        mock_idp.discovery_overrides["id_token_signing_alg_values_supported"] = ["HS256", "none"]
        with pytest.raises(SsoDiscoveryInvalidError) as exc:
            parse_discovery(mock_idp.discovery_document(), expected_issuer=mock_idp.issuer)
        assert exc.value.reason == "no_allowed_signing_alg"

    def test_algorithms_default_to_rs256_when_absent(self, mock_idp: MockIdP) -> None:
        mock_idp.discovery_overrides["id_token_signing_alg_values_supported"] = OMIT
        meta = parse_discovery(mock_idp.discovery_document(), expected_issuer=mock_idp.issuer)
        assert meta.id_token_signing_alg_values_supported == ("RS256",)

    def test_algorithms_must_be_a_list(self, mock_idp: MockIdP) -> None:
        mock_idp.discovery_overrides["id_token_signing_alg_values_supported"] = "RS256"
        with pytest.raises(SsoDiscoveryInvalidError):
            parse_discovery(mock_idp.discovery_document(), expected_issuer=mock_idp.issuer)

    def test_pkce_without_s256_refused(self, mock_idp: MockIdP) -> None:
        mock_idp.discovery_overrides["code_challenge_methods_supported"] = ["plain"]
        with pytest.raises(SsoDiscoveryInvalidError) as exc:
            parse_discovery(mock_idp.discovery_document(), expected_issuer=mock_idp.issuer)
        assert exc.value.reason == "pkce_s256_unsupported"

    def test_pkce_methods_absent_is_accepted(self, mock_idp: MockIdP) -> None:
        mock_idp.discovery_overrides["code_challenge_methods_supported"] = OMIT
        meta = parse_discovery(mock_idp.discovery_document(), expected_issuer=mock_idp.issuer)
        assert meta.code_challenge_methods_supported == ()

    def test_optional_flags_default_false(self, mock_idp: MockIdP) -> None:
        for key in ("authorization_response_iss_parameter_supported", "backchannel_logout_supported"):
            mock_idp.discovery_overrides[key] = OMIT
        mock_idp.discovery_overrides["userinfo_endpoint"] = OMIT
        mock_idp.discovery_overrides["end_session_endpoint"] = OMIT
        meta = parse_discovery(mock_idp.discovery_document(), expected_issuer=mock_idp.issuer)
        assert meta.authorization_response_iss_parameter_supported is False
        assert meta.backchannel_logout_supported is False
        assert meta.userinfo_endpoint is None
        assert meta.end_session_endpoint is None


# ---------------------------------------------------------------------------
# get_provider_metadata (fetch + cache)
# ---------------------------------------------------------------------------


class TestGetProviderMetadata:
    async def test_fetches_and_caches(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        provider = make_provider(mock_idp)
        meta = await get_provider_metadata(provider, redis=fake_redis)
        assert meta.issuer == mock_idp.issuer
        assert mock_idp.calls["discovery"] == 1

        key = discovery_cache_key(provider.id)
        assert key == f"sso:disco:{provider.id}"
        assert fake_redis.ttl_of(key) == pytest.approx(DISCOVERY_CACHE_TTL_SECONDS)
        assert json.loads(fake_redis.store[key][0])["issuer"] == mock_idp.issuer

        # Provider row keeps a last-known-good copy.
        assert provider.discovery_cache is not None
        assert provider.discovery_cache["issuer"] == mock_idp.issuer
        assert isinstance(provider.discovery_fetched_at, datetime)
        assert provider.discovery_fetched_at.tzinfo is not None

    async def test_cache_hit_makes_no_second_request(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        provider = make_provider(mock_idp)
        first = await get_provider_metadata(provider, redis=fake_redis)
        second = await get_provider_metadata(provider, redis=fake_redis)
        assert first == second
        assert mock_idp.calls["discovery"] == 1

    async def test_cache_expires(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        provider = make_provider(mock_idp)
        await get_provider_metadata(provider, redis=fake_redis)
        fake_redis.advance(DISCOVERY_CACHE_TTL_SECONDS + 1)
        await get_provider_metadata(provider, redis=fake_redis)
        assert mock_idp.calls["discovery"] == 2

    async def test_force_refresh_bypasses_cache(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        provider = make_provider(mock_idp)
        await get_provider_metadata(provider, redis=fake_redis)
        await get_provider_metadata(provider, redis=fake_redis, force_refresh=True)
        assert mock_idp.calls["discovery"] == 2

    async def test_cached_document_for_another_issuer_is_ignored(
        self, mock_idp: MockIdP, fake_redis: FakeRedis
    ) -> None:
        provider = make_provider(mock_idp)
        doc = {**mock_idp.discovery_document(), "issuer": "https://old.example.com"}
        await fake_redis.set(discovery_cache_key(provider.id), json.dumps(doc), ex=3600)
        meta = await get_provider_metadata(provider, redis=fake_redis)
        assert meta.issuer == mock_idp.issuer
        assert mock_idp.calls["discovery"] == 1

    async def test_corrupt_cache_entry_is_ignored(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        provider = make_provider(mock_idp)
        await fake_redis.set(discovery_cache_key(provider.id), "{not json", ex=3600)
        meta = await get_provider_metadata(provider, redis=fake_redis)
        assert meta.issuer == mock_idp.issuer

    async def test_redis_unavailable_falls_back_to_fetching(self, mock_idp: MockIdP, broken_redis: BrokenRedis) -> None:
        provider = make_provider(mock_idp)
        meta = await get_provider_metadata(provider, redis=broken_redis)  # type: ignore[arg-type]
        assert meta.issuer == mock_idp.issuer
        assert mock_idp.calls["discovery"] == 1
        assert broken_redis.calls >= 1

    async def test_explicit_discovery_url_is_used(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        provider = make_provider(mock_idp, discovery_url=mock_idp.discovery_url)
        await get_provider_metadata(provider, redis=fake_redis)
        assert mock_idp.calls["discovery"] == 1

    async def test_issuer_mismatch_from_idp(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        mock_idp.discovery_overrides["issuer"] = "https://attacker.example.net/realms/test"
        provider = make_provider(mock_idp)
        with pytest.raises(SsoDiscoveryInvalidError) as exc:
            await get_provider_metadata(provider, redis=fake_redis)
        assert exc.value.reason == "issuer_mismatch"
        assert discovery_cache_key(provider.id) not in fake_redis.store
        assert provider.discovery_cache is None

    async def test_http_issuer_refused_without_flag(self, fake_redis: FakeRedis) -> None:
        idp = MockIdP(issuer="http://idp.example.com/realms/test")
        provider = make_provider(idp)
        with override_transport(idp.transport), pytest.raises(SsoIdpUnreachableError) as exc:
            await get_provider_metadata(provider, redis=fake_redis)
        # The HTTP client itself refuses the http:// discovery URL.
        assert exc.value.reason == "insecure_url"
        assert idp.calls["discovery"] == 0

    async def test_http_issuer_allowed_with_flag(self, fake_redis: FakeRedis) -> None:
        idp = MockIdP(issuer="http://idp.example.com/realms/test")
        provider = make_provider(idp)
        with override_transport(idp.transport):
            meta = await get_provider_metadata(
                provider, redis=fake_redis, settings=_settings(sso_allow_insecure_http=True)
            )
        assert meta.issuer == idp.issuer

    @pytest.mark.parametrize("status", [500, 502, 503, 429])
    async def test_server_error_is_unreachable(self, mock_idp: MockIdP, fake_redis: FakeRedis, status: int) -> None:
        mock_idp.set_fault("discovery", status=status)
        with pytest.raises(SsoIdpUnreachableError) as exc:
            await get_provider_metadata(make_provider(mock_idp), redis=fake_redis)
        assert exc.value.code == "sso_idp_unreachable"
        assert exc.value.reason == f"http_{status}"

    async def test_not_found_is_invalid(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        mock_idp.set_fault("discovery", status=404)
        with pytest.raises(SsoDiscoveryInvalidError) as exc:
            await get_provider_metadata(make_provider(mock_idp), redis=fake_redis)
        assert exc.value.reason == "http_404"

    async def test_timeout_is_unreachable(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        mock_idp.set_fault("discovery", timeout=True)
        with pytest.raises(SsoIdpUnreachableError) as exc:
            await get_provider_metadata(make_provider(mock_idp), redis=fake_redis)
        assert exc.value.reason == "timeout"

    async def test_redirect_to_another_host_refused(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        mock_idp.set_fault("discovery", redirect_to="https://evil.example.net/.well-known/openid-configuration")
        with pytest.raises(SsoIdpUnreachableError) as exc:
            await get_provider_metadata(make_provider(mock_idp), redis=fake_redis)
        assert exc.value.reason == "redirect_refused"

    async def test_oversized_document_refused(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        mock_idp.set_fault("discovery", body_size=2 * 1024 * 1024)
        with pytest.raises(SsoIdpUnreachableError) as exc:
            await get_provider_metadata(make_provider(mock_idp), redis=fake_redis)
        assert exc.value.reason == "response_too_large"

    async def test_non_json_body_is_invalid(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        mock_idp.set_fault("discovery", body_size=10)
        with pytest.raises(SsoDiscoveryInvalidError) as exc:
            await get_provider_metadata(make_provider(mock_idp), redis=fake_redis)
        assert exc.value.reason == "not_json"

    async def test_unreachable_falls_back_to_last_known_good(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        provider = make_provider(mock_idp)
        await get_provider_metadata(provider, redis=fake_redis)
        await invalidate_provider_cache(provider.id, redis=fake_redis)
        mock_idp.set_fault("discovery", status=503)
        meta = await get_provider_metadata(provider, redis=fake_redis)
        assert meta.issuer == mock_idp.issuer
        assert mock_idp.calls["discovery"] == 2

    async def test_last_known_good_for_another_issuer_is_not_used(
        self, mock_idp: MockIdP, fake_redis: FakeRedis
    ) -> None:
        old = {**mock_idp.discovery_document(), "issuer": "https://old.example.com"}
        provider = make_provider(mock_idp, discovery_cache=old, discovery_fetched_at=datetime.now(UTC))
        mock_idp.set_fault("discovery", timeout=True)
        with pytest.raises(SsoIdpUnreachableError):
            await get_provider_metadata(provider, redis=fake_redis)

    async def test_invalid_document_does_not_fall_back(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        provider = make_provider(mock_idp)
        await get_provider_metadata(provider, redis=fake_redis)
        await invalidate_provider_cache(provider.id, redis=fake_redis)
        mock_idp.discovery_overrides["issuer"] = "https://attacker.example.net"
        with pytest.raises(SsoDiscoveryInvalidError):
            await get_provider_metadata(provider, redis=fake_redis)

    async def test_invalidate_removes_discovery_and_jwks(self, mock_idp: MockIdP, fake_redis: FakeRedis) -> None:
        provider = make_provider(mock_idp)
        await fake_redis.set(f"sso:jwks:{provider.id}", "{}", ex=60)
        await get_provider_metadata(provider, redis=fake_redis)
        await invalidate_provider_cache(provider.id, redis=fake_redis)
        assert discovery_cache_key(provider.id) not in fake_redis.store
        assert f"sso:jwks:{provider.id}" not in fake_redis.store

    async def test_invalidate_tolerates_redis_down(self, broken_redis: BrokenRedis) -> None:
        await invalidate_provider_cache(1, redis=broken_redis)  # type: ignore[arg-type]

    async def test_default_redis_is_the_shared_client(
        self, mock_idp: MockIdP, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _get_redis() -> FakeRedis:
            return fake_redis

        monkeypatch.setattr(discovery, "get_redis", _get_redis)
        provider = make_provider(mock_idp)
        await get_provider_metadata(provider)
        assert discovery_cache_key(provider.id) in fake_redis.store
