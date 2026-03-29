"""Internationalization (i18n) infrastructure for Specivo.

Provides:
- Per-request locale activation via ``ContextVar`` (async-safe).
- ``gettext()`` / ``ngettext()`` for immediate translation.
- ``gettext_lazy()`` / ``LazyString`` for deferred translation (module-level constants).
- Translation catalog loading and caching with multi-package merging.

Usage::

    from specivo.core.i18n import gettext as _, gettext_lazy as _l

    # Immediate translation (inside a request handler)
    message = _("Not found")

    # Lazy translation (module-level constant, evaluated per-request)
    DEFAULT_ERROR = _l("Something went wrong")
"""

from __future__ import annotations

import gettext as gettext_module
import logging
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from babel.support import Translations

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Locale ContextVar — per-task isolation for async safety
# ---------------------------------------------------------------------------

_current_locale: ContextVar[str] = ContextVar("_current_locale", default="en")


def activate(locale: str) -> None:
    """Set the active locale for the current async task / thread."""
    _current_locale.set(locale)


def deactivate() -> None:
    """Reset the active locale to the default ('en')."""
    _current_locale.set("en")


def get_locale() -> str:
    """Return the currently active locale."""
    return _current_locale.get()


# ---------------------------------------------------------------------------
# Translation catalog loading and caching
# ---------------------------------------------------------------------------

_LOCALE_DIR = Path(__file__).resolve().parent.parent / "locale"

# Extra locale directories registered by plugins: (path, domain)
_extra_locale_dirs: list[tuple[Path, str]] = []

# Cache: locale -> merged GNUTranslations
_translations_cache: dict[str, gettext_module.GNUTranslations] = {}

_CORE_DOMAIN = "specivo"


def _load_translations(locale: str) -> gettext_module.GNUTranslations:
    """Load and merge translation catalogs for *locale*.

    Loads the core catalog from ``specivo/locale/``, then merges any
    plugin catalogs registered in ``_extra_locale_dirs``.

    Results are cached in ``_translations_cache``.
    """
    if locale in _translations_cache:
        return _translations_cache[locale]

    # Load core translations
    core_trans = Translations.load(str(_LOCALE_DIR), [locale], _CORE_DOMAIN)

    # Merge plugin translations
    for locale_dir, domain in _extra_locale_dirs:
        plugin_trans = Translations.load(str(locale_dir), [locale], domain)
        # Only merge if real translations were found (not NullTranslations)
        if not isinstance(plugin_trans, gettext_module.NullTranslations) or hasattr(plugin_trans, "_catalog"):
            core_trans.merge(plugin_trans)

    _translations_cache[locale] = core_trans
    return core_trans


# ---------------------------------------------------------------------------
# Translation functions
# ---------------------------------------------------------------------------


def gettext(message: str) -> str:
    """Translate *message* using the currently active locale.

    Returns the original string when no catalog is loaded (English passthrough).
    """
    locale = get_locale()
    try:
        trans = _load_translations(locale)
        return trans.gettext(message)
    except Exception:
        return message


def ngettext(singular: str, plural: str, n: int) -> str:
    """Translate singular/plural forms based on *n*.

    Returns the appropriate English form when no catalog is loaded.
    """
    locale = get_locale()
    try:
        trans = _load_translations(locale)
        return trans.ngettext(singular, plural, n)
    except Exception:
        return singular if n == 1 else plural


# ---------------------------------------------------------------------------
# LazyString — deferred translation proxy
# ---------------------------------------------------------------------------


class LazyString:
    """A string-like object that defers translation until evaluation.

    The message is translated via ``gettext()`` only when ``__str__()``
    is called, using the locale active at that point in time.

    This is essential for module-level constants that are defined at import
    time but need to be translated per-request.
    """

    __slots__ = ("_message", "_args", "_kwargs")

    def __init__(self, message: str, args: tuple = (), kwargs: dict[str, Any] | None = None) -> None:
        self._message = message
        self._args = args
        self._kwargs = kwargs or {}

    def __str__(self) -> str:
        translated = gettext(self._message)
        if self._args or self._kwargs:
            return translated.format(*self._args, **self._kwargs)
        return translated

    def __repr__(self) -> str:
        return f"LazyString({self._message!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return str(self) == other
        if isinstance(other, LazyString):
            return str(self) == str(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(str(self))

    def __bool__(self) -> bool:
        return bool(str(self))

    def __len__(self) -> int:
        return len(str(self))

    def __contains__(self, item: str) -> bool:
        return item in str(self)

    def __add__(self, other: str) -> str:
        return str(self) + other

    def __radd__(self, other: str) -> str:
        return other + str(self)

    def __mod__(self, other: Any) -> str:
        return str(self) % other

    def format(self, *args: Any, **kwargs: Any) -> str:
        """Format the translated string with the given arguments."""
        translated = gettext(self._message)
        return translated.format(*args, **kwargs)


def gettext_lazy(message: str) -> LazyString:
    """Return a ``LazyString`` that will be translated on evaluation."""
    return LazyString(message)
