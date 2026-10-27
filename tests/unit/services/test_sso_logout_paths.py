"""Where a signed-out browser lands: the stealth prefix is part of the address."""

from __future__ import annotations

import pytest

from specivo.core.config import Settings, get_settings
from specivo.services.sso.logout import LOGGED_OUT_PATH, logged_out_path, post_logout_redirect_uri

pytestmark = pytest.mark.unit


def _settings(prefix: str) -> Settings:
    return get_settings().model_copy(update={"app_url": "https://specivo.example.com/", "stealth_prefix": prefix})


def test_without_a_prefix() -> None:
    settings = _settings("")
    assert logged_out_path(settings) == LOGGED_OUT_PATH == "/login/?logged_out=1"
    assert post_logout_redirect_uri(settings) == "https://specivo.example.com/login/?logged_out=1"


@pytest.mark.parametrize("prefix", ["/hidden", "/hidden/"])
def test_with_a_stealth_prefix(prefix: str) -> None:
    settings = _settings(prefix)
    assert logged_out_path(settings) == "/hidden/login/?logged_out=1"
    assert post_logout_redirect_uri(settings) == "https://specivo.example.com/hidden/login/?logged_out=1"
