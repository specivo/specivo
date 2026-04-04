"""Versioned WebSocket message envelope."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WSEnvelope:
    """Versioned envelope for all WebSocket messages.

    All WS messages use this format so clients can handle protocol
    evolution via the ``v`` field.
    """

    v: int
    type: str
    payload: dict
    ts: str

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dict."""
        return {
            "v": self.v,
            "type": self.type,
            "payload": self.payload,
            "ts": self.ts,
        }
