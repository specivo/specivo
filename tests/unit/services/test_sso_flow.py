"""Unit tests for the SSO login transaction (``specivo.services.sso.flow``).

The transaction is the relying party's half of the login CSRF and mix-up
defences: ``state``, ``nonce`` and the PKCE verifier live in Redis under the
hash of ``state``, are bound to the browser through the ``spv_sso_tx``
cookie, and can be completed exactly once.
"""

from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from specivo.core.config import get_settings
from specivo.services.sso.discovery import parse_discovery
from specivo.services.sso.errors import SsoStateInvalidError, SsoUnavailableError
from specivo.services.sso.flow import (
    ALLOWED_EXTRA_AUTH_PARAMS,
    TX_TTL_SECONDS,
    backchannel_logout_uri_for,
    begin,
    complete,
    redirect_uri_for,
    tx_cookie_path,
    tx_key,
)
from specivo.testing.oidc_mock import MockIdP, s256
from tests.unit.services.conftest import BrokenRedis, FakeRedis, make_provider

pytestmark = pytest.mark.unit


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _single(url: str) -> dict[str, str]:
    params = _query(url)
    assert all(len(values) == 1 for values in params.values()), params
    return {key: values[0] for key, values in params.items()}


@pytest.fixture
def idp() -> MockIdP:
    return MockIdP()


@pytest.fixture
def metadata(idp: MockIdP):
    return parse_discovery(idp.discovery_document(), expected_issuer=idp.issuer)


# ---------------------------------------------------------------------------
# redirect_uri and cookie path
# ---------------------------------------------------------------------------


def test_redirect_uri_is_app_url_plus_slug() -> None:
    settings = get_settings().model_copy(update={"app_url": "https://specivo.example.com/", "stealth_prefix": ""})
    assert redirect_uri_for("corp-idp", settings) == "https://specivo.example.com/auth/oidc/corp-idp/callback/"


def test_redirect_uri_and_cookie_path_respect_the_stealth_prefix() -> None:
    settings = get_settings().model_copy(
        update={"app_url": "https://specivo.example.com", "stealth_prefix": "/hidden/"}
    )
    assert redirect_uri_for("corp-idp", settings) == "https://specivo.example.com/hidden/auth/oidc/corp-idp/callback/"
    assert tx_cookie_path(settings) == "/hidden/auth/oidc/"


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("", "https://specivo.example.com/auth/oidc/corp-idp/backchannel-logout/"),
        ("/hidden/", "https://specivo.example.com/hidden/auth/oidc/corp-idp/backchannel-logout/"),
    ],
)
def test_backchannel_logout_uri_respects_the_stealth_prefix(prefix: str, expected: str) -> None:
    settings = get_settings().model_copy(update={"app_url": "https://specivo.example.com/", "stealth_prefix": prefix})
    assert backchannel_logout_uri_for("corp-idp", settings) == expected


def test_tx_key_is_the_sha256_of_the_state() -> None:
    assert tx_key("abc") == "sso:tx:" + hashlib.sha256(b"abc").hexdigest()


# ---------------------------------------------------------------------------
# begin
# ---------------------------------------------------------------------------


async def test_begin_builds_a_pkce_s256_authorize_url(idp: MockIdP, metadata, fake_redis: FakeRedis) -> None:
    provider = make_provider(idp)
    request = await begin(provider, metadata, next_path="/projects/ACME/", redis=fake_redis)

    assert request.url.startswith(idp.authorization_endpoint + "?")
    params = _single(request.url)
    assert params["response_type"] == "code"
    assert params["client_id"] == idp.client_id
    assert params["redirect_uri"] == redirect_uri_for(provider.slug)
    assert params["scope"].split() == ["openid", "profile", "email"]
    assert params["code_challenge_method"] == "S256"
    assert len(params["state"]) >= 43
    assert len(params["nonce"]) >= 43

    raw = fake_redis.store[tx_key(params["state"])][0]
    record = json.loads(raw)
    # The challenge is the S256 of the stored verifier; the verifier is 43-128 chars.
    assert 43 <= len(record["code_verifier"]) <= 128
    assert params["code_challenge"] == s256(record["code_verifier"])
    assert record["nonce"] == params["nonce"]
    assert record["provider_id"] == provider.id
    assert record["issuer"] == idp.issuer
    assert record["next"] == "/projects/ACME/"
    assert record["intent"] == "login"
    assert record["binding_hash"] == hashlib.sha256(request.binding_token.encode()).hexdigest()
    # The state itself is never stored, only its hash is the key.
    assert params["state"] not in raw
    assert fake_redis.ttl_of(tx_key(params["state"])) == pytest.approx(TX_TTL_SECONDS)


async def test_begin_reduces_an_offsite_next_to_nothing(idp: MockIdP, metadata, fake_redis: FakeRedis) -> None:
    provider = make_provider(idp)
    for hostile in ("https://evil.example.com/", "//evil.example.com/", "/\\evil.example.com", "javascript:alert(1)"):
        request = await begin(provider, metadata, next_path=hostile, redis=fake_redis)
        record = json.loads(fake_redis.store[tx_key(_single(request.url)["state"])][0])
        assert record["next"] == ""


async def test_begin_enforces_openid_scope(idp: MockIdP, metadata, fake_redis: FakeRedis) -> None:
    provider = make_provider(idp, scopes="profile email")
    request = await begin(provider, metadata, redis=fake_redis)
    assert _single(request.url)["scope"].split()[0] == "openid"


async def test_begin_adds_only_allowlisted_extra_params(idp: MockIdP, metadata, fake_redis: FakeRedis) -> None:
    provider = make_provider(
        idp,
        extra_auth_params={
            "prompt": "login",
            "max_age": 3600,
            "login_hint": "member@example.com",
            # Not allowlisted: must never reach the IdP or override core parameters.
            "redirect_uri": "https://attacker.example.net/",
            "response_type": "token",
            "code_challenge_method": "plain",
            "nested": {"a": 1},
        },
    )
    request = await begin(provider, metadata, redis=fake_redis)
    params = _single(request.url)
    assert params["prompt"] == "login"
    assert params["max_age"] == "3600"
    assert params["login_hint"] == "member@example.com"
    assert params["redirect_uri"] == redirect_uri_for(provider.slug)
    assert params["response_type"] == "code"
    assert params["code_challenge_method"] == "S256"
    assert "nested" not in params
    assert set(ALLOWED_EXTRA_AUTH_PARAMS) == {"prompt", "domain_hint", "login_hint", "acr_values", "max_age"}


async def test_begin_keeps_a_query_already_in_the_authorization_endpoint(idp: MockIdP, fake_redis: FakeRedis) -> None:
    idp.discovery_overrides["authorization_endpoint"] = idp.authorization_endpoint + "?tenant=acme"
    metadata = parse_discovery(idp.discovery_document(), expected_issuer=idp.issuer)
    request = await begin(make_provider(idp), metadata, redis=fake_redis)
    params = _single(request.url)
    assert params["tenant"] == "acme"
    assert params["response_type"] == "code"


async def test_each_begin_uses_fresh_secrets(idp: MockIdP, metadata, fake_redis: FakeRedis) -> None:
    provider = make_provider(idp)
    first = await begin(provider, metadata, redis=fake_redis)
    second = await begin(provider, metadata, redis=fake_redis)
    a, b = _single(first.url), _single(second.url)
    assert a["state"] != b["state"]
    assert a["nonce"] != b["nonce"]
    assert a["code_challenge"] != b["code_challenge"]
    assert first.binding_token != second.binding_token


async def test_begin_fails_closed_without_redis(idp: MockIdP, metadata, broken_redis: BrokenRedis) -> None:
    with pytest.raises(SsoUnavailableError) as exc_info:
        await begin(make_provider(idp), metadata, redis=broken_redis)
    assert exc_info.value.code == "sso_unavailable"


def test_secrets_stay_out_of_repr() -> None:
    from specivo.services.sso.flow import AuthorizationRequest, LoginTransaction

    request = AuthorizationRequest(url="https://idp/auth?state=s3cr3t", binding_token="b1nd")
    assert "s3cr3t" not in repr(request) and "b1nd" not in repr(request)
    tx = LoginTransaction(
        provider_id=1,
        provider_slug="x",
        issuer="https://idp",
        nonce="n0nce",
        code_verifier="v3rifier",
        redirect_uri="https://app/cb",
        next_path="",
        intent="login",
        user_id=None,
    )
    assert "n0nce" not in repr(tx) and "v3rifier" not in repr(tx)


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


async def test_complete_returns_the_transaction_once(idp: MockIdP, metadata, fake_redis: FakeRedis) -> None:
    provider = make_provider(idp)
    request = await begin(provider, metadata, next_path="/wiki/", redis=fake_redis)
    params = _single(request.url)

    tx = await complete(state=params["state"], binding_token=request.binding_token, redis=fake_redis)
    assert tx.provider_id == provider.id
    assert tx.provider_slug == provider.slug
    assert tx.issuer == idp.issuer
    assert tx.nonce == params["nonce"]
    assert s256(tx.code_verifier) == params["code_challenge"]
    assert tx.redirect_uri == params["redirect_uri"]
    assert tx.next_path == "/wiki/"
    assert tx.intent == "login"
    assert tx.user_id is None
    assert ("getdel", tx_key(params["state"])) in fake_redis.ops

    with pytest.raises(SsoStateInvalidError):
        await complete(state=params["state"], binding_token=request.binding_token, redis=fake_redis)


async def test_complete_refuses_another_browser(idp: MockIdP, metadata, fake_redis: FakeRedis) -> None:
    request = await begin(make_provider(idp), metadata, redis=fake_redis)
    state = _single(request.url)["state"]
    with pytest.raises(SsoStateInvalidError) as exc_info:
        await complete(state=state, binding_token="someone-elses-cookie", redis=fake_redis)
    assert exc_info.value.code == "sso_state_invalid"
    # The record is burnt: the right browser cannot use it afterwards either.
    with pytest.raises(SsoStateInvalidError):
        await complete(state=state, binding_token=request.binding_token, redis=fake_redis)


@pytest.mark.parametrize("binding", [None, ""])
async def test_complete_refuses_a_missing_cookie(idp: MockIdP, metadata, fake_redis: FakeRedis, binding) -> None:
    request = await begin(make_provider(idp), metadata, redis=fake_redis)
    with pytest.raises(SsoStateInvalidError):
        await complete(state=_single(request.url)["state"], binding_token=binding, redis=fake_redis)


@pytest.mark.parametrize("state", [None, "", "x" * 600])
async def test_complete_refuses_a_missing_or_odd_state(fake_redis: FakeRedis, state) -> None:
    with pytest.raises(SsoStateInvalidError):
        await complete(state=state, binding_token="b", redis=fake_redis)


async def test_complete_refuses_an_expired_transaction(idp: MockIdP, metadata, fake_redis: FakeRedis) -> None:
    request = await begin(make_provider(idp), metadata, redis=fake_redis)
    fake_redis.advance(TX_TTL_SECONDS + 1)
    with pytest.raises(SsoStateInvalidError):
        await complete(state=_single(request.url)["state"], binding_token=request.binding_token, redis=fake_redis)


async def test_complete_refuses_a_corrupt_record(fake_redis: FakeRedis) -> None:
    await fake_redis.set(tx_key("s"), "not json", ex=60)
    with pytest.raises(SsoStateInvalidError):
        await complete(state="s", binding_token="b", redis=fake_redis)


async def test_complete_fails_closed_without_redis(broken_redis: BrokenRedis) -> None:
    with pytest.raises(SsoUnavailableError):
        await complete(state="s" * 43, binding_token="b", redis=broken_redis)


# ---------------------------------------------------------------------------
# Re-authentication: link forces it, max_age is remembered for auth_time
# ---------------------------------------------------------------------------


async def test_link_forces_reauthentication(idp: MockIdP, metadata, fake_redis: FakeRedis) -> None:
    provider = make_provider(
        idp, extra_auth_params={"prompt": "consent", "max_age": 3600, "login_hint": "member@example.com"}
    )
    request = await begin(provider, metadata, intent="link", user_id=7, redis=fake_redis)
    params = _single(request.url)
    # The link values win over the provider's configured ones; other extras stay.
    assert params["prompt"] == "login"
    assert params["max_age"] == "0"
    assert params["login_hint"] == "member@example.com"

    tx = await complete(state=params["state"], binding_token=request.binding_token, redis=fake_redis)
    assert tx.intent == "link"
    assert tx.max_age == 0


async def test_login_sends_and_records_the_configured_max_age(idp: MockIdP, metadata, fake_redis: FakeRedis) -> None:
    provider = make_provider(idp, extra_auth_params={"max_age": "300"})
    request = await begin(provider, metadata, redis=fake_redis)
    params = _single(request.url)
    assert params["max_age"] == "300"
    assert "prompt" not in params
    tx = await complete(state=params["state"], binding_token=request.binding_token, redis=fake_redis)
    assert tx.max_age == 300


async def test_login_without_max_age_records_none(idp: MockIdP, metadata, fake_redis: FakeRedis) -> None:
    request = await begin(make_provider(idp), metadata, redis=fake_redis)
    params = _single(request.url)
    assert "max_age" not in params
    tx = await complete(state=params["state"], binding_token=request.binding_token, redis=fake_redis)
    assert tx.max_age is None
