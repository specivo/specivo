"""Unit tests for installed-locale discovery and real catalog translation.

These tests exercise the compiled ``.mo`` catalogs that ship with the
core package (es, fr, ru, zh) plus the English source language. They do
not touch the database.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# get_available_locales — the canonical "languages we can render" list
# ---------------------------------------------------------------------------


def test_available_locales_are_the_installed_catalogs():
    """Only languages with a compiled core catalog + a label are offered."""
    from specivo.core.locales import get_available_locales

    assert get_available_locales() == ["en", "es", "fr", "ru", "th", "zh"]


def test_thai_is_available():
    """Thai ships a compiled core catalog and must be offered."""
    from specivo.core.locales import get_available_locales

    assert "th" in get_available_locales()


# ---------------------------------------------------------------------------
# Real translation via the compiled catalogs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("locale", ["es", "fr", "ru", "zh"])
def test_translation_differs_from_english(locale: str):
    """Activating a non-English locale translates a known msgid away from English."""
    from specivo.core.i18n import activate, deactivate, gettext

    activate(locale)
    try:
        translated = gettext("Save")
    finally:
        deactivate()

    assert translated != "Save", f"{locale!r} catalog did not translate 'Save'"
    assert translated  # non-empty


def test_english_is_passthrough():
    """The English locale returns the source string unchanged."""
    from specivo.core.i18n import activate, deactivate, gettext

    activate("en")
    try:
        assert gettext("Save") == "Save"
    finally:
        deactivate()


@pytest.mark.parametrize("locale", ["es", "fr", "ru", "th", "zh"])
def test_named_placeholders_survive_translation(locale: str):
    """A translation keeps exactly the ``%(name)s`` placeholders of its msgid.

    Strings handed to Alpine components are filled in by JavaScript with
    ``replace('%(name)s', value)``, so a renamed or dropped placeholder would
    leave raw ``%(...)s`` text, or lose the value, in the page.
    """
    import re
    from pathlib import Path

    from babel.messages.pofile import read_po

    import specivo

    placeholder = re.compile(r"%\((\w+)\)[sd]")
    po_path = Path(specivo.__file__).parent / "locale" / locale / "LC_MESSAGES" / "specivo.po"
    with po_path.open("rb") as fh:
        catalog = read_po(fh, locale=locale)

    mismatched = [
        message.id
        for message in catalog
        if isinstance(message.id, str)
        and message.id
        and message.string
        and set(placeholder.findall(message.id)) != set(placeholder.findall(message.string))
    ]
    assert mismatched == [], f"{locale}: placeholders differ from the msgid in {mismatched}"


def test_expected_native_translations():
    """Spot-check the actual translated values per language."""
    from specivo.core.i18n import activate, deactivate, gettext

    expected = {
        "es": "Guardar",
        "fr": "Enregistrer",
        "th": "บันทึก",
        "zh": "保存",
    }
    for locale, word in expected.items():
        activate(locale)
        try:
            assert gettext("Save") == word
        finally:
            deactivate()
