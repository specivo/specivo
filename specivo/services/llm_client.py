"""A minimal client for OpenAI-compatible chat completions, used by Specivo's own background AI features.

``POST {base_url}/chat/completions`` with a JSON body, ``Authorization: Bearer``
when the provider has a key. Hosted services and local servers (Ollama, vLLM,
LM Studio, llama.cpp) answer the same shape, so one client covers a cloud
model and an air-gapped one.

Safety:

- Redirects are never followed, so a provider cannot bounce the request (and
  its key) somewhere else.
- The answer is read up to ``CONCEPT_EXTRACTION_MAX_RESPONSE_BYTES``; a longer
  one is an error.
- Errors carry a short message of our own (the HTTP status, "not JSON"), never
  the provider's body, which could echo the key or the content sent.
- The answer text is returned as data. Callers treat it as untrusted input.

Tests never reach a real model: they pass an ``httpx`` transport, or replace
the client with a fake that has the same ``complete`` method.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from specivo.core.constants import CONCEPT_EXTRACTION_MAX_RESPONSE_BYTES, CONCEPT_EXTRACTION_TIMEOUT_SECONDS

_MAX_TOKEN_COUNT = 10_000_000


class LLMError(Exception):
    """A call that gave no usable answer. ``input_tokens`` / ``output_tokens`` when the provider still billed it."""

    def __init__(self, message: str, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


@dataclass(frozen=True, slots=True)
class ChatResult:
    """The answer text and the tokens the provider says the call used."""

    text: str
    input_tokens: int
    output_tokens: int


class ChatClient(Protocol):
    """What the extraction needs from a model: one chat completion that answers JSON."""

    async def complete(self, messages: list[dict[str, str]], *, max_tokens: int) -> ChatResult: ...


def chat_completions_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return min(value, _MAX_TOKEN_COUNT)


def _usage(body: Any) -> tuple[int, int]:
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return 0, 0
    return _count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens"))


class OpenAICompatibleClient:
    """Chat completions against one endpoint and model."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = CONCEPT_EXTRACTION_TIMEOUT_SECONDS,
    ) -> None:
        self.url = chat_completions_url(base_url)
        self.model = model
        self._api_key = api_key
        self._transport = transport
        self._timeout = timeout

    async def complete(self, messages: list[dict[str, str]], *, max_tokens: int) -> ChatResult:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with (
                httpx.AsyncClient(timeout=self._timeout, follow_redirects=False, transport=self._transport) as client,
                client.stream("POST", self.url, json=payload, headers=headers) as response,
            ):
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > CONCEPT_EXTRACTION_MAX_RESPONSE_BYTES:
                        raise LLMError("The provider's answer is too large")
                status = response.status_code
        except httpx.TimeoutException:
            raise LLMError("The provider did not answer in time") from None
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach the provider ({type(exc).__name__})") from None
        try:
            body = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            body = None
        input_tokens, output_tokens = _usage(body)
        if status != 200:
            raise LLMError(
                f"The provider answered HTTP {status}", input_tokens=input_tokens, output_tokens=output_tokens
            )
        text = _answer_text(body)
        if text is None:
            raise LLMError(
                "The provider's answer has no message text", input_tokens=input_tokens, output_tokens=output_tokens
            )
        return ChatResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens)


def _answer_text(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None
