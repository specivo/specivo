"""The MCP session lifetime settings reach the mounted session manager."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from specivo.core.config import Settings, get_settings
from specivo.mcp import mount

pytestmark = [pytest.mark.unit]


def test_mounted_manager_uses_the_configured_idle_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "mcp_session_idle_timeout_seconds", 4321)
    monkeypatch.setattr(mount, "_session_manager", None)
    mount.mount_mcp(FastAPI())
    manager = mount.get_mcp_session_manager()
    assert manager is not None
    assert manager.session_idle_timeout == 4321


def test_session_lifetimes_have_generous_defaults() -> None:
    fields = Settings.model_fields
    assert fields["mcp_session_ttl_seconds"].default == 7 * 24 * 60 * 60
    assert fields["mcp_session_idle_timeout_seconds"].default == 60 * 60


@pytest.mark.parametrize("name", ["mcp_session_ttl_seconds", "mcp_session_idle_timeout_seconds"])
def test_session_lifetimes_must_be_positive(name: str) -> None:
    with pytest.raises(ValidationError):
        Settings(secret_key="x" * 40, **{name: 0})
