"""Unit tests for i18n infrastructure.

RED phase -- these tests define the expected behavior of ``specivo.core.i18n``:

- Locale activation via ContextVar (async-safe, per-request)
- ``gettext()`` / ``_()`` passthrough when no catalog is loaded
- ``LazyString`` deferred translation proxy
- ``ngettext()`` singular/plural handling
- ``load_translations()`` / ``_load_translations()`` catalog loading

All tests are pure -- no database, no I/O, no .po/.mo files required.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Locale activation (ContextVar)
# ---------------------------------------------------------------------------


class TestLocaleActivation:
    def test_default_locale_is_en(self):
        """get_locale() returns 'en' when no locale has been explicitly activated."""
        from specivo.core.i18n import get_locale

        assert get_locale() == "en"

    def test_activate_sets_locale(self):
        """activate() changes the locale returned by get_locale()."""
        from specivo.core.i18n import activate, deactivate, get_locale

        activate("th")
        try:
            assert get_locale() == "th"
        finally:
            deactivate()

    def test_deactivate_resets_locale(self):
        """deactivate() restores the locale to the default 'en'."""
        from specivo.core.i18n import activate, deactivate, get_locale

        activate("th")
        assert get_locale() == "th"
        deactivate()
        assert get_locale() == "en"

    @pytest.mark.asyncio
    async def test_locale_is_async_safe(self):
        """Two concurrent coroutines with different locales don't interfere.

        ContextVar provides per-task isolation, so activating 'th' in one
        coroutine must not affect the locale seen by another coroutine.
        """
        from specivo.core.i18n import activate, deactivate, get_locale

        results: dict[str, str] = {}
        barrier = asyncio.Barrier(2)

        async def task_a():
            activate("th")
            await barrier.wait()  # sync with task_b
            results["a"] = get_locale()
            deactivate()

        async def task_b():
            activate("ja")
            await barrier.wait()  # sync with task_a
            results["b"] = get_locale()
            deactivate()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(task_a())
            tg.create_task(task_b())

        assert results["a"] == "th"
        assert results["b"] == "ja"


# ---------------------------------------------------------------------------
# gettext passthrough (no catalog loaded)
# ---------------------------------------------------------------------------


class TestGettextPassthrough:
    def test_gettext_returns_original_without_catalog(self):
        """When no .mo catalog is loaded, gettext returns the original string."""
        from specivo.core.i18n import gettext

        assert gettext("Hello") == "Hello"

    def test_gettext_with_format(self):
        """gettext result supports str.format() for interpolation."""
        from specivo.core.i18n import gettext

        result = gettext("Hello {name}").format(name="World")
        assert result == "Hello World"


# ---------------------------------------------------------------------------
# LazyString
# ---------------------------------------------------------------------------


class TestLazyString:
    def test_lazy_string_evaluates_on_str(self):
        """str(lazy) calls gettext at evaluation time, not at definition time."""
        from specivo.core.i18n import gettext_lazy

        lazy = gettext_lazy("Not found")
        # At this point no translation happened yet; str() triggers it
        assert str(lazy) == "Not found"

    def test_lazy_string_uses_active_locale(self):
        """LazyString translates using the locale active at str() time."""
        from specivo.core.i18n import activate, deactivate, gettext_lazy

        lazy = gettext_lazy("Not found")

        # Patch gettext to simulate a catalog that translates "Not found" -> "ไม่พบ"
        with patch("specivo.core.i18n.gettext", return_value="ไม่พบ"):
            activate("th")
            result = str(lazy)
            deactivate()

        assert result == "ไม่พบ"

    def test_lazy_string_format(self):
        """LazyString.format() works like str.format() after translation."""
        from specivo.core.i18n import gettext_lazy

        lazy = gettext_lazy("Hello {name}")
        assert lazy.format(name="World") == "Hello World"

    def test_lazy_string_is_not_str_at_definition(self):
        """LazyString is its own type, not a plain str."""
        from specivo.core.i18n import LazyString, gettext_lazy

        lazy = gettext_lazy("test")
        assert type(lazy) is LazyString
        assert not isinstance(lazy, str)


# ---------------------------------------------------------------------------
# ngettext (plural forms)
# ---------------------------------------------------------------------------


class TestNgettext:
    def test_ngettext_singular(self):
        """ngettext returns the singular form when n == 1."""
        from specivo.core.i18n import ngettext

        result = ngettext("{n} issue", "{n} issues", 1)
        assert result == "{n} issue"

    def test_ngettext_plural(self):
        """ngettext returns the plural form when n != 1."""
        from specivo.core.i18n import ngettext

        result = ngettext("{n} issue", "{n} issues", 5)
        assert result == "{n} issues"


# ---------------------------------------------------------------------------
# load_translations (catalog merging)
# ---------------------------------------------------------------------------


class TestLoadTranslations:
    def test_load_translations_with_empty_dirs(self):
        """When no locale dirs exist, translation still works (passthrough)."""
        from specivo.core.i18n import gettext

        # No .mo files present in test environment -- should not crash
        result = gettext("Some string")
        assert result == "Some string"

    @staticmethod
    def _write_catalog(root, locale: str, domain: str, messages: dict[str, str]) -> None:
        """Compile *messages* into ``root/<locale>/LC_MESSAGES/<domain>.mo``."""
        from babel.messages.catalog import Catalog
        from babel.messages.mofile import write_mo

        catalog = Catalog(locale=locale, domain=domain)
        for msgid, msgstr in messages.items():
            catalog.add(msgid, msgstr)
        mo_dir = root / locale / "LC_MESSAGES"
        mo_dir.mkdir(parents=True)
        with (mo_dir / f"{domain}.mo").open("wb") as fh:
            write_mo(fh, catalog)

    def _load(self, tmp_path, locale: str):
        from specivo.core.i18n import _load_translations

        with (
            patch("specivo.core.i18n._LOCALE_DIR", tmp_path / "core"),
            patch("specivo.core.i18n._extra_locale_dirs", [(tmp_path / "plugin", "specivo_plugin")]),
            patch("specivo.core.i18n._translations_cache", {}),
        ):
            return _load_translations(locale)

    def test_plugin_catalog_is_merged_into_core(self, tmp_path):
        self._write_catalog(tmp_path / "core", "th", "specivo", {"Issues": "core-th-issues"})
        self._write_catalog(tmp_path / "plugin", "th", "specivo_plugin", {"Boards": "plugin-th-boards"})

        trans = self._load(tmp_path, "th")

        assert trans.gettext("Issues") == "core-th-issues"
        assert trans.gettext("Boards") == "plugin-th-boards"

    def test_plugin_catalog_is_used_when_the_locale_has_no_core_catalog(self, tmp_path):
        (tmp_path / "core").mkdir()
        self._write_catalog(tmp_path / "plugin", "th", "specivo_plugin", {"Boards": "plugin-th-boards"})

        trans = self._load(tmp_path, "th")

        assert trans.gettext("Boards") == "plugin-th-boards"
        assert trans.gettext("Issues") == "Issues"

    def test_missing_plugin_catalog_leaves_core_unchanged(self, tmp_path):
        self._write_catalog(tmp_path / "core", "th", "specivo", {"Issues": "core-th-issues"})
        (tmp_path / "plugin").mkdir()

        trans = self._load(tmp_path, "th")

        assert trans.gettext("Issues") == "core-th-issues"
