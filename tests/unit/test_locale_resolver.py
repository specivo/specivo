"""Unit tests for locale resolution precedence.

Covers the layered fallback in ``LocaleMiddleware._detect_locale`` and the
admin default-language override stored in ``core.runtime_settings``:

    user.language  >  specivo_lang cookie  >  Accept-Language
                   >  admin DB default     >  config default  >  'en'

The authenticated-user override itself is applied in
``web.deps.get_current_user_optional`` (after auth resolves the user), so
the per-user layer is asserted via the end-to-end integration tests; here we
assert the middleware-level precedence and the runtime override.
"""

from __future__ import annotations

import pytest

from specivo.core.middleware import LocaleMiddleware

pytestmark = pytest.mark.unit


class _FakeSettings:
    def __init__(self, default_language: str, available_languages: list[str]) -> None:
        self.default_language = default_language
        self.available_languages = available_languages


def _scope(*, cookie: str | None = None, accept_language: str | None = None) -> dict:
    headers: list[tuple[bytes, bytes]] = []
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))
    if accept_language is not None:
        headers.append((b"accept-language", accept_language.encode()))
    return {"type": "http", "headers": headers}


@pytest.fixture(autouse=True)
def _reset_override():
    """Ensure each test starts and ends with no admin override set."""
    from specivo.core.runtime_settings import set_default_language_override

    set_default_language_override(None)
    yield
    set_default_language_override(None)


def _resolve(scope: dict, settings: _FakeSettings) -> str:
    mw = LocaleMiddleware(app=lambda *a, **k: None)
    return mw._detect_locale(scope, settings)


# ---------------------------------------------------------------------------
# Admin DB default beats config default
# ---------------------------------------------------------------------------


def test_admin_default_beats_config_default():
    from specivo.core.runtime_settings import set_default_language_override

    settings = _FakeSettings("en", ["en", "es", "fr", "ru", "zh"])
    set_default_language_override("fr")

    # No cookie, no Accept-Language → admin override wins over config default.
    assert _resolve(_scope(), settings) == "fr"


def test_config_default_used_when_no_override():
    settings = _FakeSettings("es", ["en", "es", "fr", "ru", "zh"])
    assert _resolve(_scope(), settings) == "es"


# ---------------------------------------------------------------------------
# Cookie beats the admin/config defaults
# ---------------------------------------------------------------------------


def test_cookie_beats_admin_default():
    from specivo.core.runtime_settings import set_default_language_override

    settings = _FakeSettings("en", ["en", "es", "fr", "ru", "zh"])
    set_default_language_override("fr")

    assert _resolve(_scope(cookie="specivo_lang=ru"), settings) == "ru"


# ---------------------------------------------------------------------------
# Unavailable code falls back to 'en'
# ---------------------------------------------------------------------------


def test_unavailable_admin_default_falls_back_to_en():
    from specivo.core.runtime_settings import set_default_language_override

    settings = _FakeSettings("en", ["en", "es", "fr", "ru", "th", "zh"])
    # 'de' has no catalog and is not in available_languages.
    set_default_language_override("de")

    assert _resolve(_scope(), settings) == "en"


def test_unavailable_config_default_falls_back_to_en():
    settings = _FakeSettings("de", ["en", "es", "fr", "ru", "th", "zh"])
    assert _resolve(_scope(), settings) == "en"


def test_unavailable_cookie_is_ignored():
    settings = _FakeSettings("es", ["en", "es", "fr", "ru", "th", "zh"])
    # Cookie names an unavailable language → ignored, fall through to default.
    assert _resolve(_scope(cookie="specivo_lang=de"), settings) == "es"
