"""Unit tests for writing to someone else: their language, their timezone, public links.

Covers ``recipient_language`` / ``recipient_locale`` / ``format_datetime_for``
in ``specivo.core.i18n`` and ``public_url`` in ``specivo.core.app_url``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from specivo.core.app_url import public_url
from specivo.core.config import get_settings
from specivo.core.i18n import (
    activate,
    deactivate,
    format_datetime_for,
    get_locale,
    gettext,
    recipient_language,
    recipient_locale,
)
from specivo.core.runtime_settings import get_default_language_override, set_default_language_override

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_locale():
    previous = get_default_language_override()
    yield
    set_default_language_override(previous)
    deactivate()


def test_offered_preference_wins():
    assert recipient_language("th") == "th"


@pytest.mark.parametrize("value", [None, "", "xx", 42])
def test_missing_or_unknown_preference_falls_back_to_the_default(value, monkeypatch):
    monkeypatch.setattr(get_settings(), "default_language", "th")
    assert recipient_language(value) == "th"


def test_admin_override_is_the_default(monkeypatch):
    monkeypatch.setattr(get_settings(), "default_language", "es")
    set_default_language_override("th")
    assert recipient_language(None) == "th"


def test_default_not_offered_falls_back_to_english(monkeypatch):
    monkeypatch.setattr(get_settings(), "default_language", "xx")
    assert recipient_language(None) == "en"


def test_recipient_locale_translates_inside_and_restores_after():
    activate("fr")
    with recipient_locale("th") as language:
        assert language == "th"
        assert gettext("Save") == "บันทึก"
    assert get_locale() == "fr"


def test_recipient_locale_restores_after_an_error():
    activate("es")
    with pytest.raises(RuntimeError), recipient_locale("th"):
        raise RuntimeError
    assert get_locale() == "es"


def test_time_in_the_recipients_timezone():
    moment = datetime(2026, 11, 6, 14, 5, tzinfo=UTC)
    assert format_datetime_for(moment, "Asia/Bangkok", "en") == "Nov 6, 2026, 21:05 (Asia/Bangkok)"
    assert format_datetime_for(moment, "UTC", "en") == "Nov 6, 2026, 14:05 (UTC)"


@pytest.mark.parametrize("zone", [None, "", "Not/AZone", "../etc/passwd"])
def test_unknown_timezone_is_utc(zone):
    moment = datetime(2026, 11, 6, 14, 5)  # naive: taken as UTC
    assert format_datetime_for(moment, zone, "en") == "Nov 6, 2026, 14:05 (UTC)"


@pytest.mark.parametrize(
    ("app_url", "prefix", "expected"),
    [
        ("https://specivo.example.com", "", "https://specivo.example.com/issue/NER-1/"),
        ("https://specivo.example.com/", "/hidden/", "https://specivo.example.com/hidden/issue/NER-1/"),
        ("http://localhost:8030", "", "http://localhost:8030/issue/NER-1/"),
        ("", "", None),
        ("not a url", "", None),
    ],
)
def test_public_url(app_url, prefix, expected, monkeypatch):
    monkeypatch.setattr(get_settings(), "app_url", app_url)
    monkeypatch.setattr(get_settings(), "stealth_prefix", prefix)
    assert public_url("/issue/NER-1/") == expected
