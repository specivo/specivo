"""ASGI plumbing for driving the MCP transports without a network server.

The MCP session tests talk to the session manager (and the legacy SSE app)
at the ASGI level so they can do what a real client connection cannot be
made to do on demand: hold a stream open forever without reading it (a
half-open TCP connection after the client machine went to sleep), or drop
it abruptly with no close handshake.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from specivo.core.constants import MCP_PENDING_CLIENT_ID
from specivo.mcp.auth import key_principal

INIT_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "test", "version": "1.0"},
}


def authenticated_user(raw_key: str) -> AuthenticatedUser:
    """What the bearer auth middleware attaches for a valid ``spv_`` key."""
    return AuthenticatedUser(
        AccessToken(
            token=raw_key,
            client_id=MCP_PENDING_CLIENT_ID,
            scopes=["mcp"],
            subject=key_principal(raw_key),
        )
    )


def http_scope(
    method: str,
    *,
    raw_key: str,
    session_id: str | None = None,
    path: str = "/",
    query_string: bytes = b"",
    accept: bytes = b"application/json, text/event-stream",
) -> dict[str, Any]:
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"accept", accept),
        (b"user-agent", b"pytest-mcp"),
    ]
    if session_id is not None:
        headers.append((b"mcp-session-id", session_id.encode("ascii")))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "headers": headers,
        "query_string": query_string,
        "user": authenticated_user(raw_key),
    }


def body_receive(body: dict[str, Any] | None):  # type: ignore[no-untyped-def]
    """A ``receive`` that yields one request body, then waits like an idle peer."""
    raw = json.dumps(body).encode() if body is not None else b""
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        # A finished request whose client is still connected: block until
        # the response is done and the task is cancelled.
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    return receive


class Capture:
    """Collects an ASGI response: status, headers and JSON-RPC payloads."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.started = anyio.Event()
        self.body_seen = anyio.Event()

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        if message["type"] == "http.response.start":
            self.started.set()
        elif message["type"] == "http.response.body" and message.get("body"):
            self.body_seen.set()

    @property
    def status(self) -> int | None:
        for m in self.messages:
            if m["type"] == "http.response.start":
                return int(m["status"])
        return None

    def header(self, name: bytes) -> str | None:
        for m in self.messages:
            if m["type"] == "http.response.start":
                for k, v in m.get("headers", []):
                    if k.lower() == name:
                        return v.decode("latin-1")
        return None

    @property
    def session_id(self) -> str | None:
        return self.header(b"mcp-session-id")

    @property
    def raw_body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.messages if m["type"] == "http.response.body")

    def events(self) -> list[tuple[str, str]]:
        """``(event, data)`` pairs of an SSE body (``message`` when unnamed)."""
        out: list[tuple[str, str]] = []
        for block in self.raw_body.decode().replace("\r\n", "\n").split("\n\n"):
            name, data = "message", []
            for line in block.split("\n"):
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].strip())
            if data:
                out.append((name, "\n".join(data)))
        return out

    def json_messages(self) -> list[dict[str, Any]]:
        """Every JSON-RPC message in the body, whether plain JSON or SSE."""
        raw = self.raw_body.strip()
        if raw.startswith(b"{"):
            return [json.loads(raw)]
        out = []
        for name, data in self.events():
            if name == "message" and data.startswith("{"):
                out.append(json.loads(data))
        return out

    def reply(self, request_id: int) -> dict[str, Any] | None:
        for msg in self.json_messages():
            if msg.get("id") == request_id:
                return msg
        return None


class OpenStream:
    """A long-lived GET whose client can vanish silently or hang up.

    ``receive`` blocks until :meth:`hang_up` delivers ``http.disconnect``.
    Without ``hang_up`` it models a half-open connection: the server never
    learns the client is gone.
    """

    def __init__(self) -> None:
        self.capture = Capture()
        self._disconnect = anyio.Event()
        self._body_sent = False

    async def receive(self) -> dict[str, Any]:
        if not self._body_sent:
            # A GET has an empty body, which the server delivers up front.
            self._body_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    def hang_up(self) -> None:
        self._disconnect.set()


async def wait_for(predicate, timeout: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    """Poll ``predicate`` until it is truthy or fail after ``timeout`` seconds."""
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.01)
