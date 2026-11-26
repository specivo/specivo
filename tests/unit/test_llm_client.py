"""The OpenAI-compatible client against a mock transport: request shape, usage, and every way a call can fail.

No real provider is ever called.
"""

from __future__ import annotations

import json

import httpx
import pytest

from specivo.core.constants import CONCEPT_EXTRACTION_MAX_RESPONSE_BYTES
from specivo.services.llm_client import LLMError, OpenAICompatibleClient

pytestmark = [pytest.mark.unit, pytest.mark.asyncio(loop_scope="function")]

MESSAGES = [{"role": "user", "content": "hello"}]


def _client(handler, api_key: str | None = "sk-test") -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        "https://models.example.com/v1/", "small-model", api_key, transport=httpx.MockTransport(handler)
    )


async def test_a_completion_returns_the_text_and_the_usage() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": '{"concepts": []}'}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 8},
            },
        )

    result = await _client(handler).complete(MESSAGES, max_tokens=100)
    assert result.text == '{"concepts": []}'
    assert (result.input_tokens, result.output_tokens) == (120, 8)
    assert seen["url"] == "https://models.example.com/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "small-model" and seen["body"]["max_tokens"] == 100
    assert seen["body"]["response_format"] == {"type": "json_object"}


async def test_no_key_sends_no_authorization_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    result = await _client(handler, api_key=None).complete(MESSAGES, max_tokens=10)
    assert (result.input_tokens, result.output_tokens) == (0, 0)


async def test_an_error_status_never_echoes_the_provider_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key sk-test for hello", "usage": {"prompt_tokens": 3}})

    with pytest.raises(LLMError) as raised:
        await _client(handler).complete(MESSAGES, max_tokens=10)
    assert str(raised.value) == "The provider answered HTTP 401"
    assert raised.value.input_tokens == 3


async def test_a_redirect_is_not_followed() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://elsewhere.example.com/collect"})

    with pytest.raises(LLMError, match="HTTP 302"):
        await _client(handler).complete(MESSAGES, max_tokens=10)
    assert calls == ["https://models.example.com/v1/chat/completions"]


async def test_an_oversized_answer_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (CONCEPT_EXTRACTION_MAX_RESPONSE_BYTES + 10))

    with pytest.raises(LLMError, match="too large"):
        await _client(handler).complete(MESSAGES, max_tokens=10)


@pytest.mark.parametrize(
    "body",
    [b"not json", b"{}", b'{"choices": []}', b'{"choices": [{"message": {"content": 42}}]}'],
)
async def test_an_answer_without_message_text_is_an_error(body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    with pytest.raises(LLMError, match="no message text"):
        await _client(handler).complete(MESSAGES, max_tokens=10)


async def test_network_failures_and_timeouts_are_errors() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMError, match="Could not reach"):
        await _client(refused).complete(MESSAGES, max_tokens=10)
    with pytest.raises(LLMError, match="in time"):
        await _client(slow).complete(MESSAGES, max_tokens=10)


async def test_bogus_usage_counts_are_ignored() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": -5, "completion_tokens": True},
            },
        )

    result = await _client(handler).complete(MESSAGES, max_tokens=10)
    assert (result.input_tokens, result.output_tokens) == (0, 0)
