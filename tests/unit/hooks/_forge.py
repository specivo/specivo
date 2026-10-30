"""Helpers for the forge provider tests: fixture loading and signatures."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "forge"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture(name: str) -> dict[str, Any]:
    return json.loads(fixture_bytes(name))


def hmac_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
