"""Fixtures for the SSO protocol-layer unit tests (no database).

``fake_redis`` is a small in-memory stand-in for the ``redis.asyncio`` calls
the SSO services make (``get``, ``set`` with ``ex``/``nx``, ``getdel``, ``delete``), with a
controllable clock for TTLs. ``broken_redis`` raises on every call, for the
"Redis unavailable" paths. ``sso_provider`` is a transient ``SsoProvider`` row
pointing at the ``mock_idp`` fixture.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Iterator
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from specivo.core import secret_box
from specivo.models.sso import SsoProvider
from specivo.testing.oidc_mock import DEFAULT_CLIENT_SECRET, MockIdP

_provider_ids = itertools.count(9_000_001)


class FakeRedis:
    """Enough of ``redis.asyncio.Redis`` (decode_responses=True) for the SSO caches."""

    def __init__(self) -> None:
        self.now = time.monotonic()
        self.store: dict[str, tuple[str, float | None]] = {}
        self.ops: list[tuple[str, str]] = []

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def _live(self, key: str) -> str | None:
        item = self.store.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and expires <= self.now:
            del self.store[key]
            return None
        return value

    def ttl_of(self, key: str) -> float | None:
        item = self.store.get(key)
        return None if item is None or item[1] is None else item[1] - self.now

    async def get(self, key: str) -> str | None:
        self.ops.append(("get", key))
        return self._live(key)

    async def set(self, key: str, value: Any, ex: int | None = None, nx: bool = False) -> bool | None:
        self.ops.append(("set", key))
        if nx and self._live(key) is not None:
            return None
        self.store[key] = (str(value), self.now + ex if ex else None)
        return True

    async def getdel(self, key: str) -> str | None:
        self.ops.append(("getdel", key))
        value = self._live(key)
        self.store.pop(key, None)
        return value

    async def delete(self, *keys: str) -> int:
        self.ops.append(("delete", ",".join(keys)))
        return sum(1 for key in keys if self.store.pop(key, None) is not None)


class BrokenRedis:
    """Every call fails like a Redis server that is down."""

    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        async def _fail(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            raise RedisConnectionError("Redis is down")

        return _fail


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def broken_redis() -> BrokenRedis:
    return BrokenRedis()


def make_provider(idp: MockIdP, **overrides: Any) -> SsoProvider:
    """A transient provider row for ``idp`` (never flushed; ids are unique per test run)."""
    values: dict[str, Any] = {
        "id": next(_provider_ids),
        "slug": "test-idp",
        "display_name": "Test IdP",
        "enabled": True,
        "issuer": idp.issuer,
        "discovery_url": None,
        "client_id": idp.client_id,
        "token_endpoint_auth_method": idp.token_endpoint_auth_method,
        "client_secret_encrypted": (
            secret_box.encrypt(idp.client_secret, "sso-client-secret") if idp.client_secret else None
        ),
        "client_secret_env": None,
        "scopes": "openid profile email",
        "claim_config": {},
        "discovery_cache": None,
        "discovery_fetched_at": None,
    }
    values.update(overrides)
    return SsoProvider(**values)


@pytest.fixture
def sso_provider(mock_idp: MockIdP) -> Iterator[SsoProvider]:
    yield make_provider(mock_idp)


__all__ = ["DEFAULT_CLIENT_SECRET", "BrokenRedis", "FakeRedis", "make_provider"]
