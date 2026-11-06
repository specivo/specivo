"""The client an agent session records: User-Agent parsing and the MCP client middleware."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from specivo.mcp.client_context import ClientInfoMiddleware, client_from_request_context, mcp_client_var
from specivo.services.agent_session_service import ClientInfo, parse_user_agent

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("user_agent", "expected"),
    [
        ("claude-code/2.1.0 (external, cli)", ClientInfo("claude-code", "2.1.0")),
        ("claude-code/1.0 (Claude opus-4)", ClientInfo("claude-code", "1.0")),
        ("python-httpx/0.28.1", ClientInfo("python-httpx", "0.28.1")),
        ("curl", ClientInfo("curl", None)),
        ("  codex/0.9  ", ClientInfo("codex", "0.9")),
        ("(no product)", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_user_agent(user_agent: str | None, expected: ClientInfo | None) -> None:
    assert parse_user_agent(user_agent) == expected


def test_client_info_fits_the_columns() -> None:
    info = ClientInfo.of("n" * 300, "v" * 300)
    assert info is not None
    assert len(info.name) == 100
    assert info.version is not None and len(info.version) == 50


def test_client_info_needs_a_name() -> None:
    assert ClientInfo.of("   ", "1.0") is None
    assert ClientInfo.of(None) is None
    assert ClientInfo.of("gemini-cli", "  ") == ClientInfo("gemini-cli", None)


def _ctx(client_info: tuple[str, str] | None = None, user_agent: str | None = None, request_id: Any = 1) -> Any:
    params = None
    if client_info is not None:
        params = SimpleNamespace(client_info=SimpleNamespace(name=client_info[0], version=client_info[1]))
    headers = {"user-agent": user_agent} if user_agent is not None else {}
    return SimpleNamespace(
        session=SimpleNamespace(client_params=params),
        request=SimpleNamespace(headers=headers),
        request_id=request_id,
    )


def test_client_info_from_the_handshake_wins_over_the_user_agent() -> None:
    ctx = _ctx(client_info=("claude-code", "2.1.0"), user_agent="node-fetch/1.0")
    assert client_from_request_context(ctx) == ClientInfo("claude-code", "2.1.0")


def test_user_agent_is_the_fallback_without_a_handshake() -> None:
    # A session rehydrated after a restart has no initialize params.
    assert client_from_request_context(_ctx(user_agent="codex/0.9")) == ClientInfo("codex", "0.9")


def test_no_client_without_handshake_or_headers() -> None:
    ctx = SimpleNamespace(session=SimpleNamespace(client_params=None), request=None, request_id=1)
    assert client_from_request_context(ctx) is None


async def test_middleware_publishes_the_client_for_the_request_only() -> None:
    seen: list[ClientInfo | None] = []

    async def call_next(ctx: Any) -> dict:
        seen.append(mcp_client_var.get())
        return {}

    await ClientInfoMiddleware()(_ctx(client_info=("claude-code", "2.1.0")), call_next)

    assert seen == [ClientInfo("claude-code", "2.1.0")]
    assert mcp_client_var.get() is None


async def test_middleware_resets_the_client_when_the_handler_fails() -> None:
    async def call_next(ctx: Any) -> dict:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await ClientInfoMiddleware()(_ctx(client_info=("claude-code", "2.1.0")), call_next)
    assert mcp_client_var.get() is None


async def test_middleware_leaves_notifications_alone() -> None:
    seen: list[ClientInfo | None] = []

    async def call_next(ctx: Any) -> None:
        seen.append(mcp_client_var.get())

    await ClientInfoMiddleware()(_ctx(client_info=("claude-code", "2.1.0"), request_id=None), call_next)
    assert seen == [None]


def test_the_mcp_server_runs_the_middleware() -> None:
    from specivo.mcp.server import mcp

    assert any(isinstance(m, ClientInfoMiddleware) for m in mcp.middleware)
