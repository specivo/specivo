"""Unit tests for the token endpoint client and userinfo (``specivo.services.sso.tokens``).

The mock IdP's token endpoint is strict (client auth, PKCE, redirect_uri,
single-use codes), so a passing exchange proves the request was well formed.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import parse_qs, unquote_plus

import httpx
import pytest

from specivo.core import secret_box
from specivo.services.sso import http as sso_http
from specivo.services.sso._support import unreachable_from
from specivo.services.sso.discovery import ProviderMetadata, parse_discovery
from specivo.services.sso.errors import (
    SsoConfigurationError,
    SsoIdpUnreachableError,
    SsoTokenExchangeFailedError,
    SsoTokenInvalidError,
    TokenRejection,
)
from specivo.services.sso.tokens import (
    IdpRefreshResult,
    TokenResponse,
    exchange_code,
    fetch_userinfo,
    refresh_at_idp,
    resolve_client_secret,
)
from specivo.testing.oidc_mock import MockIdP, override_transport, pkce_pair
from tests.unit.services.conftest import make_provider

pytestmark = pytest.mark.unit

REDIRECT_URI = "https://specivo.example.com/auth/oidc/test-idp/callback/"


def _meta(idp: MockIdP) -> ProviderMetadata:
    return parse_discovery(idp.discovery_document(), expected_issuer=idp.issuer)


def _code(idp: MockIdP, **kwargs: Any) -> tuple[str, str]:
    verifier, challenge = pkce_pair()
    code = idp.issue_code(nonce=idp.default_nonce, code_challenge=challenge, redirect_uri=REDIRECT_URI, **kwargs)
    return code, verifier


class _Recorder(httpx.AsyncBaseTransport):
    """Records requests on their way to the mock IdP."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        return await self.inner.handle_async_request(request)


# ---------------------------------------------------------------------------
# Client secret resolution
# ---------------------------------------------------------------------------


class TestResolveClientSecret:
    def test_encrypted(self, mock_idp: MockIdP) -> None:
        assert resolve_client_secret(make_provider(mock_idp)) == mock_idp.client_secret

    def test_env_var(self, mock_idp: MockIdP, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SPECIVO_SSO_SECRET_TOKENS_TEST", "from-env")
        provider = make_provider(
            mock_idp, client_secret_encrypted=None, client_secret_env="SPECIVO_SSO_SECRET_TOKENS_TEST"
        )
        assert resolve_client_secret(provider) == "from-env"

    def test_env_var_missing(self, mock_idp: MockIdP, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SPECIVO_SSO_SECRET_TOKENS_TEST", raising=False)
        provider = make_provider(
            mock_idp, client_secret_encrypted=None, client_secret_env="SPECIVO_SSO_SECRET_TOKENS_TEST"
        )
        with pytest.raises(SsoConfigurationError) as exc:
            resolve_client_secret(provider)
        assert exc.value.reason == "client_secret_env_unset"

    @pytest.mark.parametrize("name", ["SECRET_KEY", "DATABASE_URL", "REDIS_URL", "SPECIVO_SSO_SECRET_", "PATH"])
    def test_env_var_outside_namespace_is_never_read(
        self, mock_idp: MockIdP, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        """A forged or legacy row must not turn the token request into an exfiltration channel."""
        monkeypatch.setenv(name, "master-secret-that-must-not-leave")
        provider = make_provider(mock_idp, client_secret_encrypted=None, client_secret_env=name)
        with pytest.raises(SsoConfigurationError) as exc:
            resolve_client_secret(provider)
        assert exc.value.reason == "client_secret_env_not_allowed"
        assert "master-secret" not in repr(exc.value)

    def test_unreadable(self, mock_idp: MockIdP) -> None:
        token = secret_box.encrypt("x", "sso-client-secret", secret_key="another-secret-key-" + "y" * 32)
        provider = make_provider(mock_idp, client_secret_encrypted=token)
        with pytest.raises(SsoConfigurationError) as exc:
            resolve_client_secret(provider)
        assert exc.value.reason == "client_secret_unreadable"

    def test_missing_for_confidential_client(self, mock_idp: MockIdP) -> None:
        provider = make_provider(mock_idp, client_secret_encrypted=None)
        with pytest.raises(SsoConfigurationError) as exc:
            resolve_client_secret(provider)
        assert exc.value.reason == "client_secret_missing"

    def test_public_client_has_none(self, mock_idp: MockIdP) -> None:
        provider = make_provider(mock_idp, token_endpoint_auth_method="none")
        assert resolve_client_secret(provider) is None

    def test_unknown_method(self, mock_idp: MockIdP) -> None:
        provider = make_provider(mock_idp, token_endpoint_auth_method="private_key_jwt")
        with pytest.raises(SsoConfigurationError) as exc:
            resolve_client_secret(provider)
        assert exc.value.reason == "unsupported_auth_method"


# ---------------------------------------------------------------------------
# Code exchange
# ---------------------------------------------------------------------------


class TestExchangeCode:
    async def test_client_secret_basic(self, mock_idp: MockIdP) -> None:
        code, verifier = _code(mock_idp)
        recorder = _Recorder(mock_idp.transport)
        with override_transport(recorder):
            result = await exchange_code(
                make_provider(mock_idp), _meta(mock_idp), code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier
            )
        assert isinstance(result, TokenResponse)
        assert result.id_token.count(".") == 2
        assert result.access_token
        assert result.token_type.lower() == "bearer"
        request = recorder.requests[0]
        assert request.headers["Authorization"].startswith("Basic ")
        assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
        assert request.headers["Accept"] == "application/json"
        form = parse_qs(request.content.decode())
        assert "client_secret" not in form
        assert form["code_verifier"] == [verifier]
        assert form["grant_type"] == ["authorization_code"]

    async def test_basic_auth_encodes_special_characters(self) -> None:
        idp = MockIdP(client_id="spec:ivo client", client_secret="p@ss:w%rd+/= ไทย")
        with override_transport(idp.transport):
            recorder = _Recorder(idp.transport)
            code, verifier = _code(idp)
            with override_transport(recorder):
                await exchange_code(
                    make_provider(idp), _meta(idp), code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier
                )
        header = recorder.requests[0].headers["Authorization"]
        decoded = base64.b64decode(header[6:]).decode("ascii")
        raw_id, _, raw_secret = decoded.partition(":")
        # RFC 6749 section 2.3.1: both parts form-urlencoded before base64.
        assert ":" not in raw_id and ":" not in raw_secret
        assert unquote_plus(raw_id) == "spec:ivo client"
        assert unquote_plus(raw_secret) == "p@ss:w%rd+/= ไทย"

    async def test_client_secret_post(self) -> None:
        idp = MockIdP(token_endpoint_auth_method="client_secret_post")
        recorder = _Recorder(idp.transport)
        code, verifier = _code(idp)
        with override_transport(recorder):
            result = await exchange_code(
                make_provider(idp), _meta(idp), code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier
            )
        assert result.id_token
        request = recorder.requests[0]
        assert "Authorization" not in request.headers
        form = parse_qs(request.content.decode())
        assert form["client_secret"] == [idp.client_secret]
        assert form["client_id"] == [idp.client_id]

    async def test_none_sends_no_secret(self) -> None:
        idp = MockIdP(client_secret=None, token_endpoint_auth_method="none")
        recorder = _Recorder(idp.transport)
        code, verifier = _code(idp)
        with override_transport(recorder):
            result = await exchange_code(
                make_provider(idp), _meta(idp), code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier
            )
        assert result.id_token
        request = recorder.requests[0]
        assert "Authorization" not in request.headers
        form = parse_qs(request.content.decode())
        assert "client_secret" not in form
        assert form["client_id"] == [idp.client_id]

    async def test_env_secret_is_used(self, mock_idp: MockIdP, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SPECIVO_SSO_SECRET_TOKENS_TEST", mock_idp.client_secret or "")
        provider = make_provider(
            mock_idp, client_secret_encrypted=None, client_secret_env="SPECIVO_SSO_SECRET_TOKENS_TEST"
        )
        code, verifier = _code(mock_idp)
        result = await exchange_code(
            provider, _meta(mock_idp), code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier
        )
        assert result.id_token

    async def test_wrong_secret_is_invalid_client(self, mock_idp: MockIdP) -> None:
        provider = make_provider(
            mock_idp, client_secret_encrypted=secret_box.encrypt("wrong-secret", "sso-client-secret")
        )
        code, verifier = _code(mock_idp)
        with pytest.raises(SsoTokenExchangeFailedError) as exc:
            await exchange_code(provider, _meta(mock_idp), code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier)
        assert exc.value.idp_error == "invalid_client"
        assert exc.value.code == "sso_token_exchange_failed"

    async def test_reused_code_is_invalid_grant(self, mock_idp: MockIdP) -> None:
        provider = make_provider(mock_idp)
        code, verifier = _code(mock_idp)
        meta = _meta(mock_idp)
        await exchange_code(provider, meta, code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier)
        with pytest.raises(SsoTokenExchangeFailedError) as exc:
            await exchange_code(provider, meta, code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier)
        assert exc.value.idp_error == "invalid_grant"
        assert exc.value.reason == "idp_error"

    async def test_wrong_verifier_is_invalid_grant(self, mock_idp: MockIdP) -> None:
        code, _ = _code(mock_idp)
        other_verifier, _ = pkce_pair()
        with pytest.raises(SsoTokenExchangeFailedError) as exc:
            await exchange_code(
                make_provider(mock_idp),
                _meta(mock_idp),
                code=code,
                redirect_uri=REDIRECT_URI,
                code_verifier=other_verifier,
            )
        assert exc.value.idp_error == "invalid_grant"

    async def test_wrong_redirect_uri_is_invalid_grant(self, mock_idp: MockIdP) -> None:
        code, verifier = _code(mock_idp)
        with pytest.raises(SsoTokenExchangeFailedError):
            await exchange_code(
                make_provider(mock_idp),
                _meta(mock_idp),
                code=code,
                redirect_uri=REDIRECT_URI + "x",
                code_verifier=verifier,
            )

    @pytest.mark.parametrize("fault", [{"status": 500}, {"status": 503}, {"timeout": True}])
    async def test_unreachable(self, mock_idp: MockIdP, fault: dict[str, Any]) -> None:
        mock_idp.set_fault("token", **fault)
        code, verifier = _code(mock_idp)
        with pytest.raises(SsoIdpUnreachableError) as exc:
            await exchange_code(
                make_provider(mock_idp), _meta(mock_idp), code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier
            )
        assert exc.value.code == "sso_idp_unreachable"

    async def test_redirect_is_not_followed_off_host(self, mock_idp: MockIdP) -> None:
        mock_idp.set_fault("token", redirect_to="https://evil.example.net/token")
        code, verifier = _code(mock_idp)
        with pytest.raises(SsoIdpUnreachableError) as exc:
            await exchange_code(
                make_provider(mock_idp), _meta(mock_idp), code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier
            )
        assert exc.value.reason == "redirect_refused"

    async def test_redirect_status_is_refused(self, mock_idp: MockIdP) -> None:
        # Same-host redirect: httpx would turn the POST into a GET; we refuse any 3xx.
        mock_idp.set_fault("token", redirect_to=mock_idp.userinfo_endpoint)
        code, verifier = _code(mock_idp)
        with pytest.raises(SsoTokenExchangeFailedError) as exc:
            await exchange_code(
                make_provider(mock_idp), _meta(mock_idp), code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier
            )
        assert exc.value.reason in ("unexpected_status", "idp_error")

    async def test_error_without_json(self, mock_idp: MockIdP) -> None:
        mock_idp.set_fault("token", status=400, body_size=20)
        code, verifier = _code(mock_idp)
        with pytest.raises(SsoTokenExchangeFailedError) as exc:
            await exchange_code(
                make_provider(mock_idp), _meta(mock_idp), code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier
            )
        assert exc.value.idp_error is None
        assert exc.value.reason == "unexpected_status"

    async def test_idp_error_code_is_sanitised(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "<script>alert(1)</script>", "error_description": "x" * 5000})

        idp = MockIdP()
        with override_transport(httpx.MockTransport(handler)):
            with pytest.raises(SsoTokenExchangeFailedError) as exc:
                await exchange_code(
                    make_provider(idp), _meta(idp), code="c", redirect_uri=REDIRECT_URI, code_verifier="v"
                )
        assert exc.value.idp_error == "invalid_error_code"
        assert "<script>" not in exc.value.message

    @pytest.mark.parametrize(
        "body",
        [
            {"access_token": "a", "token_type": "Bearer"},
            {"id_token": "", "access_token": "a", "token_type": "Bearer"},
            {"id_token": 5, "access_token": "a", "token_type": "Bearer"},
            {"id_token": "a.b.c", "access_token": "a", "token_type": "MAC"},
            {"id_token": "a.b.c", "access_token": 7, "token_type": "Bearer"},
            {"id_token": "a.b.c", "token_type": "Bearer"},
            ["not", "an", "object"],
        ],
    )
    async def test_invalid_success_response(self, body: Any) -> None:
        idp = MockIdP()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        with override_transport(httpx.MockTransport(handler)):
            with pytest.raises(SsoTokenInvalidError) as exc:
                await exchange_code(
                    make_provider(idp), _meta(idp), code="c", redirect_uri=REDIRECT_URI, code_verifier="v"
                )
        assert exc.value.reason == TokenRejection.TOKEN_RESPONSE_INVALID

    async def test_success_response_not_json(self) -> None:
        idp = MockIdP()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>")

        with override_transport(httpx.MockTransport(handler)):
            with pytest.raises(SsoTokenInvalidError) as exc:
                await exchange_code(
                    make_provider(idp), _meta(idp), code="c", redirect_uri=REDIRECT_URI, code_verifier="v"
                )
        assert exc.value.reason == TokenRejection.TOKEN_RESPONSE_INVALID

    async def test_token_response_repr_hides_tokens(self, mock_idp: MockIdP) -> None:
        code, verifier = _code(mock_idp)
        result = await exchange_code(
            make_provider(mock_idp), _meta(mock_idp), code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier
        )
        text = repr(result) + str(result)
        assert result.id_token not in text
        assert result.access_token not in text


# ---------------------------------------------------------------------------
# Userinfo
# ---------------------------------------------------------------------------


class TestRefreshAtIdp:
    async def test_code_exchange_returns_the_refresh_token(self, mock_idp: MockIdP) -> None:
        code, verifier = _code(mock_idp)
        result = await exchange_code(
            make_provider(mock_idp), _meta(mock_idp), code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier
        )
        assert result.refresh_token
        assert result.refresh_token not in repr(result)

    async def test_no_refresh_token_issued(self, mock_idp: MockIdP) -> None:
        mock_idp.issue_refresh_tokens = False
        code, verifier = _code(mock_idp)
        result = await exchange_code(
            make_provider(mock_idp), _meta(mock_idp), code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier
        )
        assert result.refresh_token is None

    async def test_accepted_without_rotation(self, mock_idp: MockIdP) -> None:
        recorder = _Recorder(mock_idp.transport)
        token = mock_idp.issue_refresh_token()
        with override_transport(recorder):
            result = await refresh_at_idp(make_provider(mock_idp), _meta(mock_idp), token)
        assert result == IdpRefreshResult(refresh_token=token)
        form = parse_qs(recorder.requests[0].content.decode())
        assert form == {"grant_type": ["refresh_token"], "refresh_token": [token]}
        assert recorder.requests[0].headers["Authorization"].startswith("Basic ")

    async def test_rotation_returns_the_new_token(self, mock_idp: MockIdP) -> None:
        mock_idp.rotate_refresh_tokens = True
        token = mock_idp.issue_refresh_token()
        result = await refresh_at_idp(make_provider(mock_idp), _meta(mock_idp), token)
        assert result.refresh_token and result.refresh_token != token
        assert token not in repr(result) and result.refresh_token not in repr(result)

    async def test_client_secret_post(self) -> None:
        idp = MockIdP(token_endpoint_auth_method="client_secret_post")
        recorder = _Recorder(idp.transport)
        with override_transport(recorder):
            await refresh_at_idp(make_provider(idp), _meta(idp), idp.issue_refresh_token())
        form = parse_qs(recorder.requests[0].content.decode())
        assert form["client_secret"] == [idp.client_secret]
        assert "Authorization" not in recorder.requests[0].headers

    async def test_revoked_token_is_invalid_grant(self, mock_idp: MockIdP) -> None:
        token = mock_idp.issue_refresh_token()
        mock_idp.revoke_refresh_tokens()
        with pytest.raises(SsoTokenExchangeFailedError) as exc:
            await refresh_at_idp(make_provider(mock_idp), _meta(mock_idp), token)
        assert exc.value.idp_error == "invalid_grant"

    async def test_wrong_secret_is_invalid_client(self, mock_idp: MockIdP) -> None:
        provider = make_provider(mock_idp)
        token = mock_idp.issue_refresh_token()
        mock_idp.client_secret = "rotated-at-the-idp"
        with pytest.raises(SsoTokenExchangeFailedError) as exc:
            await refresh_at_idp(provider, _meta(mock_idp), token)
        assert exc.value.idp_error == "invalid_client"

    @pytest.mark.parametrize("fault", [{"status": 500}, {"status": 429}, {"timeout": True}])
    async def test_unreachable(self, mock_idp: MockIdP, fault: dict[str, Any]) -> None:
        mock_idp.set_fault("token", **fault)
        with pytest.raises(SsoIdpUnreachableError):
            await refresh_at_idp(make_provider(mock_idp), _meta(mock_idp), mock_idp.issue_refresh_token())

    @pytest.mark.parametrize("content", [b"not json", b"[]", b'{"token_type": "Bearer"}'])
    async def test_unusable_success_is_unreachable(self, content: bytes) -> None:
        idp = MockIdP()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                return httpx.Response(200, content=content)
            return idp.transport.handle_request(request)

        with override_transport(httpx.MockTransport(handler)):
            with pytest.raises(SsoIdpUnreachableError) as exc:
                await refresh_at_idp(make_provider(idp), _meta(idp), "a-refresh-token")
        assert exc.value.reason == "invalid_response"

    async def test_refusal_never_logs_the_token(self, mock_idp: MockIdP, caplog: pytest.LogCaptureFixture) -> None:
        token = mock_idp.issue_refresh_token()
        mock_idp.revoke_refresh_tokens()
        with pytest.raises(SsoTokenExchangeFailedError):
            await refresh_at_idp(make_provider(mock_idp), _meta(mock_idp), token)
        assert token not in caplog.text
        assert "sso.refresh.failed" in caplog.text


class TestUserinfo:
    async def _tokens(self, idp: MockIdP) -> TokenResponse:
        code, verifier = _code(idp)
        return await exchange_code(
            make_provider(idp), _meta(idp), code=code, redirect_uri=REDIRECT_URI, code_verifier=verifier
        )

    async def test_fetch(self, mock_idp: MockIdP) -> None:
        tokens = await self._tokens(mock_idp)
        info = await fetch_userinfo(_meta(mock_idp), tokens.access_token, expected_sub=mock_idp.default_sub)
        assert info["sub"] == mock_idp.default_sub
        assert info["email"] == "member@example.com"

    async def test_sub_mismatch(self, mock_idp: MockIdP) -> None:
        tokens = await self._tokens(mock_idp)
        mock_idp.userinfo_overrides["sub"] = "someone-else"
        with pytest.raises(SsoTokenInvalidError) as exc:
            await fetch_userinfo(_meta(mock_idp), tokens.access_token, expected_sub=mock_idp.default_sub)
        assert exc.value.code == "sso_token_invalid"
        assert exc.value.reason == TokenRejection.SUB_MISMATCH

    async def test_missing_sub(self, mock_idp: MockIdP) -> None:
        from specivo.testing.oidc_mock import OMIT

        tokens = await self._tokens(mock_idp)
        mock_idp.userinfo_overrides["sub"] = OMIT
        with pytest.raises(SsoTokenInvalidError) as exc:
            await fetch_userinfo(_meta(mock_idp), tokens.access_token, expected_sub=mock_idp.default_sub)
        assert exc.value.reason == TokenRejection.SUB_MISMATCH

    async def test_rejected_access_token(self, mock_idp: MockIdP) -> None:
        with pytest.raises(SsoTokenInvalidError) as exc:
            await fetch_userinfo(_meta(mock_idp), "not-a-token", expected_sub=mock_idp.default_sub)
        assert exc.value.reason == TokenRejection.USERINFO_REJECTED

    async def test_no_endpoint(self, mock_idp: MockIdP) -> None:
        mock_idp.discovery_overrides["userinfo_endpoint"] = None
        from specivo.testing.oidc_mock import OMIT

        mock_idp.discovery_overrides["userinfo_endpoint"] = OMIT
        with pytest.raises(SsoConfigurationError) as exc:
            await fetch_userinfo(_meta(mock_idp), "t", expected_sub="s")
        assert exc.value.reason == "userinfo_endpoint_missing"

    @pytest.mark.parametrize("fault", [{"status": 502}, {"timeout": True}])
    async def test_unreachable(self, mock_idp: MockIdP, fault: dict[str, Any]) -> None:
        tokens = await self._tokens(mock_idp)
        mock_idp.set_fault("userinfo", **fault)
        with pytest.raises(SsoIdpUnreachableError):
            await fetch_userinfo(_meta(mock_idp), tokens.access_token, expected_sub=mock_idp.default_sub)

    @pytest.mark.parametrize(
        ("content", "content_type"),
        [(b"[1]", "application/json"), (b"nope", "application/json"), (b"a.b.c", "application/jwt")],
    )
    async def test_unusable_body(self, content: bytes, content_type: str) -> None:
        idp = MockIdP()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=content, headers={"Content-Type": content_type})

        with override_transport(httpx.MockTransport(handler)):
            with pytest.raises(SsoTokenInvalidError) as exc:
                await fetch_userinfo(_meta(idp), "t", expected_sub="s")
        assert exc.value.reason == TokenRejection.USERINFO_REJECTED

    async def test_sends_bearer_token(self, mock_idp: MockIdP) -> None:
        tokens = await self._tokens(mock_idp)
        recorder = _Recorder(mock_idp.transport)
        with override_transport(recorder):
            await fetch_userinfo(_meta(mock_idp), tokens.access_token, expected_sub=mock_idp.default_sub)
        assert recorder.requests[0].headers["Authorization"] == f"Bearer {tokens.access_token}"
        assert recorder.requests[0].method == "GET"


# ---------------------------------------------------------------------------
# Transport error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.ReadTimeout("t"), "timeout"),
        (sso_http.SSODeadlineExceededError("t"), "timeout"),
        (sso_http.SSORedirectError("r"), "redirect_refused"),
        (sso_http.SSOInsecureURLError("i"), "insecure_url"),
        (sso_http.SSOResponseTooLargeError("l"), "response_too_large"),
        (sso_http.SSOBlockedAddressError("b"), "blocked_address"),
        (httpx.TooManyRedirects("m"), "too_many_redirects"),
        (httpx.ConnectError("c"), "connection_failed"),
    ],
)
def test_unreachable_reasons(error: httpx.HTTPError, reason: str) -> None:
    mapped = unreachable_from(error, what="test")
    assert mapped.code == "sso_idp_unreachable"
    assert mapped.reason == reason
