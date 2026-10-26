"""Password hashers: the preferred bcrypt hasher, legacy hashers and their registry.

Modelled on Django's password framework. A stored ``users.password_hash`` names
the scheme that produced it, and the hasher for that scheme verifies it:

- native bcrypt hashes keep their own ``$2b$`` / ``$2a$`` / ``$2y$`` prefix, so
  every hash stored before this module existed is still recognised;
- anything else is ``<algorithm>$...``, where ``<algorithm>`` is the id of a
  legacy hasher.

Only the preferred hasher (bcrypt) ever creates a hash from a password. A legacy
hasher verifies a hash imported from another system, and after a successful
sign-in the caller replaces it with a preferred one (see
``specivo.services.auth_utils.verify_and_update``). A legacy hasher builds its
stored value at import time from the source system's raw columns, without the
plaintext, through :meth:`PasswordHasher.from_source`.

Weak source formats are never stored as they are. :class:`WrappedLegacyHasher`
wraps the source digest in bcrypt at import (``<algorithm>$<params>$<bcrypt>``),
so a leaked database holds nothing weaker than a native hash and every sign-in
costs one bcrypt whichever scheme the account is on. Pass-through storage is
reserved for formats that are already strong (Django PBKDF2, foreign bcrypt).

The registry is built once per process by :func:`build_password_hasher_registry`
— normally through ``specivo.core.plugin_manager.bootstrap`` — and is immutable
afterwards. Third-party hashers come from the ``get_password_hashers()`` plugin
hook or from dotted paths in the ``PASSWORD_HASHERS`` setting. Registering a
hasher does not enable it: an algorithm verifies only when it is also listed in
``LEGACY_PASSWORD_ALGORITHMS``; that check lives in ``auth_utils``.

Every ``verify`` here is constant-time (``bcrypt.checkpw`` or
``hmac.compare_digest``) and never raises: a malformed value is "no match".
Timing across schemes is evened out one level up: ``auth_utils.verify_password``
pads any verification that spent less than one bcrypt at ``BCRYPT_ROUNDS``, so
no hasher (a plugin's included) can answer faster than a native check.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import importlib
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from urllib.parse import quote, unquote

import bcrypt

from specivo.core.config import get_settings

logger = logging.getLogger(__name__)

# Width of ``users.password_hash``. A hasher whose stored value does not fit is
# rejected per user at import time rather than truncated.
MAX_ENCODED_LENGTH = 255

# bcrypt reads at most this many bytes of its input.
BCRYPT_MAX_BYTES = 72

# Prefixes of a native bcrypt hash. ``$2b$`` is what this library produces;
# ``$2a$`` and ``$2y$`` are the same algorithm under the names older and PHP
# implementations use.
BCRYPT_PREFIXES: tuple[str, ...] = ("$2b$", "$2a$", "$2y$")
_NATIVE_BCRYPT_PREFIX = "$2b$"

# What a legacy algorithm id may look like. Lowercase, so ids are compared
# exactly and cannot collide by case; short, so they never crowd the column.
ALGORITHM_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

# Ids no plugin or setting may claim: the preferred hasher's own name and the
# bcrypt prefixes it is identified by. Built-in ids are reserved as well, by
# being registered first.
RESERVED_ALGORITHM_IDS: frozenset[str] = frozenset({"bcrypt", "2a", "2b", "2y"})

# Where a hasher came from, as shown by ``specivo.cli.admin password-hashers``.
ORIGIN_CORE = "core"
ORIGIN_SETTING = "setting"


def plugin_origin(plugin_name: str) -> str:
    """Return the origin label for a hasher contributed by *plugin_name*."""
    return f"plugin:{plugin_name}"


class PasswordHasherError(ValueError):
    """A hasher, or the registry built from hashers, is misconfigured."""


class CredentialImportError(ValueError):
    """A source credential cannot be turned into a stored value.

    Raised by :meth:`PasswordHasher.from_source`. The message never contains the
    source digest or salt, so it is safe to put in an import report.
    """


@runtime_checkable
class PasswordHasher(Protocol):
    """A scheme that can verify a stored value.

    ``algorithm`` is the prefix stored in ``password_hash``. ``verify`` must be
    constant-time and must never raise. ``must_update`` says whether a value
    that just verified should be replaced with a preferred hash; it is always
    true for a legacy hasher. ``from_source`` is the import side: it turns a
    source system's raw credential columns into the stored value, without the
    plaintext.
    """

    algorithm: str

    def verify(self, password: str, encoded: str) -> bool: ...

    def must_update(self, encoded: str) -> bool: ...

    def from_source(self, fields: Mapping[str, str]) -> str: ...


@runtime_checkable
class PreferredHasher(PasswordHasher, Protocol):
    """The one hasher that creates new hashes from a password."""

    def encode(self, password: str) -> str: ...


def _truncate(password: str | bytes) -> bytes:
    """Return *password* as bcrypt input: UTF-8, cut at bcrypt's 72-byte limit."""
    data = password.encode("utf-8") if isinstance(password, str) else password
    return data[:BCRYPT_MAX_BYTES]


def _bcrypt_rounds(encoded: str) -> int | None:
    """Return the cost recorded in a bcrypt hash, or ``None`` if unreadable."""
    parts = encoded.split("$")
    if len(parts) < 4 or not parts[2].isdigit():
        return None
    return int(parts[2])


def is_wellformed_bcrypt(value: str) -> bool:
    """Return whether *value* has the shape of a bcrypt hash bcrypt would accept."""
    if len(value) != 60 or not value.startswith(BCRYPT_PREFIXES):
        return False
    rounds = _bcrypt_rounds(value)
    return rounds is not None and 4 <= rounds <= 31


# The highest bcrypt cost actually spent by ``_checkpw`` since the last
# ``start_cost_tracking()``. ``auth_utils.verify_password`` reads it to make
# sure every verification costs at least one bcrypt at ``BCRYPT_ROUNDS``,
# whatever hasher ran: one that does no bcrypt of its own (PBKDF2, a plugin),
# or one at a lower cost, is padded with a throwaway check.
_bcrypt_cost_spent: ContextVar[int] = ContextVar("bcrypt_cost_spent", default=0)


def start_cost_tracking() -> None:
    """Reset the record of bcrypt work done in the current context."""
    _bcrypt_cost_spent.set(0)


def bcrypt_cost_spent() -> int:
    """Return the highest bcrypt cost checked since :func:`start_cost_tracking`."""
    return _bcrypt_cost_spent.get()


def _checkpw(password: bytes, encoded: str) -> bool:
    """``bcrypt.checkpw`` that answers ``False`` instead of raising.

    Records the cost of the check once bcrypt has actually run it.
    """
    try:
        result = bcrypt.checkpw(password, encoded.encode("ascii"))
    except (ValueError, TypeError, UnicodeError):
        return False
    rounds = _bcrypt_rounds(encoded) or 0
    if rounds > _bcrypt_cost_spent.get():
        _bcrypt_cost_spent.set(rounds)
    return result


class BcryptHasher:
    """The preferred hasher: bcrypt at the configured ``BCRYPT_ROUNDS``.

    Identified by the native bcrypt prefixes rather than a ``bcrypt$`` one, so
    the hashes already in the database stay valid untouched.
    """

    algorithm = "bcrypt"

    def __init__(self, rounds: int | None = None) -> None:
        self._rounds = rounds

    @property
    def rounds(self) -> int:
        return self._rounds if self._rounds is not None else get_settings().bcrypt_rounds

    def encode(self, password: str) -> str:
        return bcrypt.hashpw(_truncate(password), bcrypt.gensalt(rounds=self.rounds)).decode("ascii")

    def verify(self, password: str, encoded: str) -> bool:
        return _checkpw(_truncate(password), encoded)

    def must_update(self, encoded: str) -> bool:
        if not encoded.startswith(_NATIVE_BCRYPT_PREFIX):
            return True
        return _bcrypt_rounds(encoded) != self.rounds

    def from_source(self, fields: Mapping[str, str]) -> str:
        raise CredentialImportError("bcrypt is the native format; import a foreign bcrypt hash as bcrypt_foreign")


# ---------------------------------------------------------------------------
# Wrapped legacy hashers
# ---------------------------------------------------------------------------


class WrappedLegacyHasher(ABC):
    """Base class for a legacy hasher whose source digest is wrapped in bcrypt.

    The secure default for a weak source format. A subclass sets ``algorithm``
    and implements two methods:

    - :meth:`source_params` (import side): pick the parameters the scheme needs
      (a salt, say) and the stored digest out of the source row's columns;
    - :meth:`inner_digest` (sign-in side): recompute that digest from a
      password and those parameters.

    Everything else is handled here: the stored form
    ``<algorithm>$<params>$<bcrypt(digest)>``, the bcrypt wrap at import, the
    constant-time check, parsing that never raises, and ``must_update`` always
    being true so the account moves to a native hash at its first sign-in.

    The wrap uses ``BCRYPT_ROUNDS``, the cost of a native hash, so checking a
    wrapped hash takes exactly as long as checking a native one.

    The default parameter encoding is ``key=value`` pairs joined by ``,`` with
    each part percent-encoded; override :meth:`encode_params` and
    :meth:`decode_params` together for a different one.
    """

    algorithm: str

    @abstractmethod
    def source_params(self, fields: Mapping[str, str]) -> tuple[dict[str, str], str]:
        """Return ``(params, digest)`` from the source row's credential columns."""

    @abstractmethod
    def inner_digest(self, password: str, params: Mapping[str, str]) -> str:
        """Recompute the source system's digest for *password*."""

    # -- parameter encoding -------------------------------------------------

    def encode_params(self, params: Mapping[str, str]) -> str:
        return ",".join(f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in sorted(params.items()))

    def decode_params(self, segment: str) -> dict[str, str]:
        params: dict[str, str] = {}
        if not segment:
            return params
        for item in segment.split(","):
            key, separator, value = item.partition("=")
            if not separator:
                raise ValueError("malformed parameter")
            params[unquote(key)] = unquote(value)
        return params

    # -- PasswordHasher -----------------------------------------------------

    def _rounds(self) -> int:
        return get_settings().bcrypt_rounds

    @staticmethod
    def _wrap_input(digest: str) -> bytes:
        """Return the bytes bcrypt wraps.

        A digest longer than bcrypt's 72-byte input would be cut short, so it is
        hashed down first. Both import and sign-in go through here, so the two
        always agree.
        """
        data = digest.encode("utf-8")
        if len(data) > BCRYPT_MAX_BYTES:
            data = hashlib.sha256(data).hexdigest().encode("ascii")
        return data

    def from_source(self, fields: Mapping[str, str]) -> str:
        try:
            params, digest = self.source_params(fields)
        except (KeyError, TypeError, ValueError) as exc:
            raise CredentialImportError(f"{self.algorithm}: source row lacks a usable credential") from exc
        if not isinstance(digest, str) or not digest:
            raise CredentialImportError(f"{self.algorithm}: source row has an empty password digest")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in params.items()):
            raise CredentialImportError(f"{self.algorithm}: parameters must be strings")

        segment = self.encode_params(params)
        if "$" in segment:
            raise CredentialImportError(f"{self.algorithm}: encoded parameters may not contain '$'")
        wrapped = bcrypt.hashpw(self._wrap_input(digest), bcrypt.gensalt(rounds=self._rounds())).decode("ascii")
        encoded = f"{self.algorithm}${segment}${wrapped}"
        if len(encoded) > MAX_ENCODED_LENGTH:
            raise CredentialImportError(f"{self.algorithm}: stored value would exceed {MAX_ENCODED_LENGTH} characters")
        return encoded

    def verify(self, password: str, encoded: str) -> bool:
        try:
            algorithm, segment, wrapped = encoded.split("$", 2)
            if algorithm != self.algorithm or not is_wellformed_bcrypt(wrapped):
                return False
            params = self.decode_params(segment)
            inner = self.inner_digest(password, params)
        except Exception:  # noqa: BLE001 - a hasher must never raise on bad input
            logger.warning("Unreadable %s password hash; treated as no match", self.algorithm)
            return False
        return _checkpw(self._wrap_input(inner), wrapped)

    def must_update(self, encoded: str) -> bool:
        return True


def sha1_hex(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()  # noqa: S324 - verifying a legacy scheme


class RedmineSHA1WrappedHasher(WrappedLegacyHasher):
    """Redmine's local-account scheme, wrapped in bcrypt.

    Redmine stores ``hashed_password = sha1_hex(salt + sha1_hex(password))``
    with a 32-hex ``salt``. Stored here as
    ``redmine_sha1$<salt>$<bcrypt(hashed_password)>`` — the salt is the only
    parameter, so it is kept bare rather than as ``salt=...``.

    Source fields: ``salt`` and ``hashed_password``.
    """

    algorithm = "redmine_sha1"

    def source_params(self, fields: Mapping[str, str]) -> tuple[dict[str, str], str]:
        salt = (fields["salt"] or "").strip()
        digest = (fields["hashed_password"] or "").strip().lower()
        if not salt or not digest:
            raise ValueError("blank salt or digest")
        return {"salt": salt}, digest

    def inner_digest(self, password: str, params: Mapping[str, str]) -> str:
        return sha1_hex(params["salt"] + sha1_hex(password))

    def encode_params(self, params: Mapping[str, str]) -> str:
        return quote(params["salt"], safe="")

    def decode_params(self, segment: str) -> dict[str, str]:
        if not segment:
            raise ValueError("missing salt")
        return {"salt": unquote(segment)}


# ---------------------------------------------------------------------------
# Pass-through legacy hashers (strong formats, stored as they are)
# ---------------------------------------------------------------------------

# Upper bound on the PBKDF2 iteration count accepted from a source. Django's
# current default is well under it; anything far above is either corrupt or an
# attempt to make every sign-in burn seconds of CPU.
_PBKDF2_MAX_ITERATIONS = 5_000_000


class DjangoPBKDF2SHA256Hasher:
    """Django's ``pbkdf2_sha256$<iterations>$<salt>$<base64 hash>``, stored as is.

    Already strong, so it is not wrapped: the stored value is
    ``django_pbkdf2_sha256$`` followed by Django's own encoding.

    Source field: ``password`` (Django's ``auth_user.password``).
    """

    algorithm = "django_pbkdf2_sha256"
    _DJANGO_PREFIX = "pbkdf2_sha256"

    @classmethod
    def _parse(cls, django_encoded: str) -> tuple[int, str, bytes]:
        scheme, iterations, salt, digest_b64 = django_encoded.split("$")
        if scheme != cls._DJANGO_PREFIX or not iterations.isdigit() or not salt:
            raise ValueError("not a pbkdf2_sha256 hash")
        rounds = int(iterations)
        if not 0 < rounds <= _PBKDF2_MAX_ITERATIONS:
            raise ValueError("iteration count out of range")
        digest = base64.b64decode(digest_b64, validate=True)
        if len(digest) != hashlib.sha256().digest_size:
            raise ValueError("wrong digest length")
        return rounds, salt, digest

    def from_source(self, fields: Mapping[str, str]) -> str:
        try:
            django_encoded = (fields["password"] or "").strip()
            self._parse(django_encoded)
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise CredentialImportError(f"{self.algorithm}: not a Django pbkdf2_sha256 hash") from exc
        encoded = f"{self.algorithm}${django_encoded}"
        if len(encoded) > MAX_ENCODED_LENGTH:
            raise CredentialImportError(f"{self.algorithm}: stored value would exceed {MAX_ENCODED_LENGTH} characters")
        return encoded

    def verify(self, password: str, encoded: str) -> bool:
        try:
            algorithm, django_encoded = encoded.split("$", 1)
            if algorithm != self.algorithm:
                return False
            rounds, salt, expected = self._parse(django_encoded)
        except (ValueError, binascii.Error):
            logger.warning("Unreadable %s password hash; treated as no match", self.algorithm)
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), rounds)
        return hmac.compare_digest(actual, expected)

    def must_update(self, encoded: str) -> bool:
        return True


class ForeignBcryptHasher:
    """A bcrypt hash from another application (PHP's ``$2y$``, ``$2a$``, ...).

    Stored as ``bcrypt_foreign$<hash>``. Namespaced rather than stored bare so
    it is still counted as legacy — enabled, reported and expired with the
    others — and replaced with a native hash at Specivo's own cost on the first
    sign-in.

    Source field: ``hash``.
    """

    algorithm = "bcrypt_foreign"

    def from_source(self, fields: Mapping[str, str]) -> str:
        try:
            value = (fields["hash"] or "").strip()
        except (KeyError, TypeError) as exc:
            raise CredentialImportError(f"{self.algorithm}: source row has no hash") from exc
        if not is_wellformed_bcrypt(value):
            raise CredentialImportError(f"{self.algorithm}: not a bcrypt hash")
        return f"{self.algorithm}${value}"

    def verify(self, password: str, encoded: str) -> bool:
        algorithm, _, value = encoded.partition("$")
        if algorithm != self.algorithm or not is_wellformed_bcrypt(value):
            return False
        return _checkpw(_truncate(password), value)

    def must_update(self, encoded: str) -> bool:
        return True


def builtin_legacy_hashers() -> list[PasswordHasher]:
    """Return a fresh instance of every legacy hasher shipped with Specivo."""
    return [RedmineSHA1WrappedHasher(), DjangoPBKDF2SHA256Hasher(), ForeignBcryptHasher()]


BUILTIN_LEGACY_ALGORITHMS: frozenset[str] = frozenset(h.algorithm for h in builtin_legacy_hashers())


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegisteredHasher:
    """A legacy hasher together with where it was registered from."""

    hasher: PasswordHasher
    origin: str


class PasswordHasherRegistry:
    """The preferred hasher plus every registered legacy hasher, immutable.

    Built by :func:`build_password_hasher_registry`, which validates every id.
    """

    def __init__(self, preferred: PreferredHasher, legacy: Iterable[RegisteredHasher] = ()) -> None:
        entries: dict[str, RegisteredHasher] = {}
        for entry in legacy:
            algorithm = getattr(entry.hasher, "algorithm", None)
            _validate_algorithm_id(algorithm, entry.origin)
            assert isinstance(algorithm, str)
            if algorithm in entries:
                raise PasswordHasherError(
                    f"Password hasher id '{algorithm}' is registered twice "
                    f"(by {entries[algorithm].origin} and by {entry.origin})"
                )
            entries[algorithm] = entry
        self._preferred = preferred
        self._entries: Mapping[str, RegisteredHasher] = MappingProxyType(entries)

    @property
    def preferred(self) -> PreferredHasher:
        return self._preferred

    def legacy(self) -> Mapping[str, RegisteredHasher]:
        """Return the legacy hashers by algorithm id (read-only)."""
        return self._entries

    def get(self, algorithm: str) -> PasswordHasher | None:
        entry = self._entries.get(algorithm)
        return entry.hasher if entry is not None else None

    def algorithm_of(self, encoded: str | None) -> str | None:
        """Return the algorithm id *encoded* is stored under, registered or not.

        ``"bcrypt"`` for a native hash, the prefix for a well-formed legacy one,
        ``None`` for anything else.
        """
        if not encoded or not isinstance(encoded, str):
            return None
        if encoded.startswith(BCRYPT_PREFIXES):
            return self._preferred.algorithm
        algorithm, separator, _ = encoded.partition("$")
        if separator and ALGORITHM_ID_PATTERN.match(algorithm):
            return algorithm
        return None

    def identify(self, encoded: str | None) -> PasswordHasher | None:
        """Return the hasher that handles *encoded*, or ``None``."""
        algorithm = self.algorithm_of(encoded)
        if algorithm is None:
            return None
        if algorithm == self._preferred.algorithm:
            return self._preferred
        return self.get(algorithm)


def _validate_algorithm_id(algorithm: object, origin: str) -> None:
    if not isinstance(algorithm, str) or not ALGORITHM_ID_PATTERN.match(algorithm):
        raise PasswordHasherError(
            f"Invalid password hasher id {algorithm!r} from {origin}: must match {ALGORITHM_ID_PATTERN.pattern}"
        )
    if algorithm in RESERVED_ALGORITHM_IDS:
        raise PasswordHasherError(f"Password hasher id '{algorithm}' from {origin} is reserved")


def _check_hasher_shape(hasher: object, origin: str) -> PasswordHasher:
    if not isinstance(hasher, PasswordHasher):
        raise PasswordHasherError(
            f"{origin} provided {hasher!r}, which is not a password hasher "
            "(needs algorithm, verify, must_update and from_source)"
        )
    return hasher


def load_hasher_from_path(path: str) -> PasswordHasher:
    """Import ``module.ClassName`` and return an instance of it."""
    module_path, _, class_name = path.rpartition(".")
    if not module_path:
        raise PasswordHasherError(f"PASSWORD_HASHERS entry {path!r} is not a dotted path")
    try:
        cls = getattr(importlib.import_module(module_path), class_name)
    except (ImportError, AttributeError) as exc:
        raise PasswordHasherError(f"Cannot load password hasher {path!r}: {exc}") from exc
    return _check_hasher_shape(cls(), f"setting PASSWORD_HASHERS ({path})")


def build_password_hasher_registry(
    plugin_hashers: Iterable[tuple[str, Iterable[object]]] = (),
    setting_paths: Iterable[str] = (),
) -> PasswordHasherRegistry:
    """Build the registry: built-ins, then plugin hashers, then setting paths.

    *plugin_hashers* is ``(plugin_name, hashers)`` per plugin. Raises
    :class:`PasswordHasherError` on an invalid, reserved or duplicate id.
    """
    entries = [RegisteredHasher(hasher, ORIGIN_CORE) for hasher in builtin_legacy_hashers()]
    for plugin_name, hashers in plugin_hashers:
        origin = plugin_origin(plugin_name)
        entries.extend(RegisteredHasher(_check_hasher_shape(h, origin), origin) for h in hashers)
    for path in setting_paths:
        entries.append(RegisteredHasher(load_hasher_from_path(path), f"{ORIGIN_SETTING}:{path}"))
    return PasswordHasherRegistry(BcryptHasher(), entries)


_registry: PasswordHasherRegistry | None = None


def install_password_hasher_registry(registry: PasswordHasherRegistry) -> None:
    """Make *registry* the process-wide one. Called by the bootstrap."""
    global _registry  # noqa: PLW0603
    _registry = registry


def get_password_hasher_registry() -> PasswordHasherRegistry:
    """Return the process-wide registry.

    A process that never ran the bootstrap (a script, a unit test) gets the
    built-ins plus the ``PASSWORD_HASHERS`` setting, built on first use.
    """
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = build_password_hasher_registry(setting_paths=get_settings().password_hashers)
    return _registry
