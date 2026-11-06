"""Unit tests for installed-locale discovery and real catalog translation.

These tests exercise the compiled ``.mo`` catalogs that ship with the
core package (es, fr, ru, th, zh) plus the English source language. They do
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


def _catalog_files() -> list:
    from pathlib import Path

    import specivo

    locale_dir = Path(specivo.__file__).parent / "locale"
    return [locale_dir / "specivo.pot", *sorted(locale_dir.glob("*/LC_MESSAGES/*.po"))]


# Locales whose catalogs must translate every msgid. English is the source
# language: its msgstr stay empty so gettext falls back to the msgid.
_SHIPPED_TRANSLATIONS = ["es", "fr", "ru", "th", "zh"]


def _read_catalog(locale: str):
    from pathlib import Path

    from babel.messages.pofile import read_po

    import specivo

    po_path = Path(specivo.__file__).parent / "locale" / locale / "LC_MESSAGES" / "specivo.po"
    with po_path.open("rb") as fh:
        return read_po(fh, locale=locale)


@pytest.mark.parametrize("locale", ["en", *_SHIPPED_TRANSLATIONS])
def test_catalog_has_no_fuzzy_entries(locale: str):
    """No entry, and not the header, is marked fuzzy.

    ``pybabel compile`` leaves fuzzy entries out of the ``.mo`` (and skips a
    catalog whose header is fuzzy), so users see English. Worse, ``pybabel
    update`` fills a fuzzy entry with the translation of a merely similar
    msgid, which turns wrong the moment someone clears the flag unread.
    Translate the entry, then drop the flag and any ``#|`` previous-msgid lines.
    """
    catalog = _read_catalog(locale)

    assert not catalog.fuzzy, f"{locale}: the catalog header is marked fuzzy"
    fuzzy = [message.id for message in catalog if message.id and message.fuzzy]
    assert fuzzy == [], f"{locale}: {len(fuzzy)} fuzzy entries: {fuzzy}"


@pytest.mark.parametrize("locale", _SHIPPED_TRANSLATIONS)
def test_catalog_translates_every_msgid(locale: str):
    """Every current msgid has a translation, including each plural form.

    Obsolete ``#~`` entries are not part of the catalog and are not checked.
    """
    catalog = _read_catalog(locale)

    untranslated = []
    for message in catalog:
        if not message.id:
            continue  # the header
        if message.pluralizable:
            forms = message.string if isinstance(message.string, tuple) else (message.string,)
            if len(forms) != catalog.num_plurals or not all(forms):
                untranslated.append(message.id[0])
        elif not message.string:
            untranslated.append(message.id)
    assert untranslated == [], f"{locale}: {len(untranslated)} untranslated entries: {untranslated}"


@pytest.mark.parametrize("path", _catalog_files(), ids=lambda p: "/".join(p.parts[-3:]))
def test_catalogs_are_well_formed(path):
    """Every catalog parses strictly, and passes ``msgfmt --check`` where gettext is installed.

    An entry missing its ``msgstr`` makes ``msgfmt`` refuse the whole file.
    """
    import shutil
    import subprocess

    from babel.messages.pofile import read_po

    with path.open("rb") as fh:
        read_po(fh, abort_invalid=True)

    msgfmt = shutil.which("msgfmt")
    if msgfmt is not None:
        result = subprocess.run(
            [msgfmt, "--check", "-o", "/dev/null", str(path)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
