"""Unit tests for the password hasher registry and the verify/upgrade helpers.

No database. bcrypt runs at its minimum cost here, so the wrapped hashes cost
nothing; the cost-dependent behaviour (``password_needs_rehash``) sets the cost
explicitly.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import bcrypt
import pytest

import specivo.services.password_hashers as hashers_mod
from specivo.core.config import get_settings
from specivo.core.plugin_manager import bootstrap
from specivo.services.auth_utils import (
    hash_password,
    password_needs_rehash,
    verify_and_update,
    verify_password,
)
from specivo.services.password_hashers import (
    MAX_ENCODED_LENGTH,
    BcryptHasher,
    CredentialImportError,
    DjangoPBKDF2SHA256Hasher,
    ForeignBcryptHasher,
    PasswordHasherError,
    RedmineSHA1WrappedHasher,
    WrappedLegacyHasher,
    build_password_hasher_registry,
    get_password_hasher_registry,
)
from tests.fixtures.acme_hashers import AcmeTrackerMd5

pytestmark = pytest.mark.unit

SALT = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
PASSWORD = "correct horse"
# sha1("correct horse") and sha1(SALT + that), computed independently of the
# hasher so the test pins Redmine's formula rather than restating the code.
SHA1_OF_PASSWORD = hashlib.sha1(PASSWORD.encode()).hexdigest()
REDMINE_DIGEST = hashlib.sha1((SALT + SHA1_OF_PASSWORD).encode()).hexdigest()


@pytest.fixture(autouse=True)
def _cheap_bcrypt_and_clean_registry(monkeypatch):
    """Run bcrypt at cost 4 and give every test a fresh registry and settings."""
    settings = get_settings()
    monkeypatch.setattr(settings, "bcrypt_rounds", 4)
    monkeypatch.setattr(settings, "legacy_password_algorithms", [])
    monkeypatch.setattr(settings, "legacy_password_hashes_expire_at", None)
    monkeypatch.setattr(settings, "legacy_password_wrap_rounds", None)
    monkeypatch.setattr(settings, "password_hashers", [])
    monkeypatch.setattr(hashers_mod, "_registry", None)
    yield


def _enable(monkeypatch, *algorithms: str) -> None:
    monkeypatch.setattr(get_settings(), "legacy_password_algorithms", list(algorithms))


def _redmine_hash() -> str:
    return RedmineSHA1WrappedHasher().from_source({"salt": SALT, "hashed_password": REDMINE_DIGEST})


def _user(password_hash: str | None) -> SimpleNamespace:
    return SimpleNamespace(id=42, password_hash=password_hash)


class _CountingCheckpw:
    """Wraps ``bcrypt.checkpw`` to count how many bcrypt checks a call costs."""

    def __init__(self, monkeypatch) -> None:
        self.calls = 0
        self._real = bcrypt.checkpw
        monkeypatch.setattr(bcrypt, "checkpw", self)

    def __call__(self, password, hashed):
        self.calls += 1
        return self._real(password, hashed)


# ---------------------------------------------------------------------------
# Redmine
# ---------------------------------------------------------------------------


# A row written by a real Redmine 7 (the importer fixture, through its own User
# model) for the fixture's placeholder password.
REAL_REDMINE_ROW = {
    "salt": "4cb900f1e98f6fd99f412388c5bdcde2",
    "hashed_password": "6fad308dc4fd61992997c64581555c79f560356a",
}
REAL_REDMINE_PASSWORD = "fixture-password-not-a-secret"


class TestRedmineHasher:
    def test_a_row_written_by_redmine_itself_verifies(self):
        encoded = RedmineSHA1WrappedHasher().from_source(REAL_REDMINE_ROW)
        assert RedmineSHA1WrappedHasher().verify(REAL_REDMINE_PASSWORD, encoded) is True
        assert RedmineSHA1WrappedHasher().verify(REAL_REDMINE_PASSWORD.upper(), encoded) is False

    def test_stored_form_wraps_the_digest_with_the_salt_in_front(self):
        encoded = _redmine_hash()
        assert encoded.startswith(f"redmine_sha1${SALT}$$2b$")
        # 12 + 1 + 32 + 1 + 60: fits even the old column width.
        assert len(encoded) == 106
        # The weak digest never sits in the database on its own.
        assert REDMINE_DIGEST not in encoded

    def test_accepts_the_right_password(self):
        assert RedmineSHA1WrappedHasher().verify(PASSWORD, _redmine_hash()) is True

    @pytest.mark.parametrize("wrong", ["correct horse ", "Correct horse", "", "wrong"])
    def test_rejects_anything_else(self, wrong):
        assert RedmineSHA1WrappedHasher().verify(wrong, _redmine_hash()) is False

    def test_an_uppercase_source_digest_still_verifies(self):
        """Redmine writes lowercase hex; a value touched by hand might not be."""
        encoded = RedmineSHA1WrappedHasher().from_source({"salt": SALT, "hashed_password": REDMINE_DIGEST.upper()})
        assert RedmineSHA1WrappedHasher().verify(PASSWORD, encoded) is True

    def test_non_ascii_password(self):
        password = "รหัสผ่านทดสอบ"
        digest = hashlib.sha1((SALT + hashlib.sha1(password.encode()).hexdigest()).encode()).hexdigest()
        encoded = RedmineSHA1WrappedHasher().from_source({"salt": SALT, "hashed_password": digest})
        assert RedmineSHA1WrappedHasher().verify(password, encoded) is True

    @pytest.mark.parametrize(
        "fields",
        [{}, {"salt": SALT}, {"hashed_password": REDMINE_DIGEST}, {"salt": "", "hashed_password": REDMINE_DIGEST}],
    )
    def test_an_incomplete_source_row_is_refused(self, fields):
        with pytest.raises(CredentialImportError) as exc:
            RedmineSHA1WrappedHasher().from_source(fields)
        assert REDMINE_DIGEST not in str(exc.value)

    def test_wrap_cost_follows_the_setting(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "legacy_password_wrap_rounds", 5)
        assert _redmine_hash().split("$")[4] == "05"

    def test_always_needs_an_update(self):
        assert RedmineSHA1WrappedHasher().must_update(_redmine_hash()) is True


# ---------------------------------------------------------------------------
# Pass-through built-ins
# ---------------------------------------------------------------------------


def _django_hash(password: str, iterations: int = 1000, salt: str = "pepperSALT") -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${base64.b64encode(digest).decode()}"


class TestDjangoHasher:
    def test_round_trip(self):
        hasher = DjangoPBKDF2SHA256Hasher()
        encoded = hasher.from_source({"password": _django_hash("s3cret pass")})
        assert encoded.startswith("django_pbkdf2_sha256$pbkdf2_sha256$1000$")
        assert hasher.verify("s3cret pass", encoded) is True
        assert hasher.verify("s3cret pas", encoded) is False

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "md5$abc$def",
            "pbkdf2_sha256$x$salt$aGVsbG8=",
            "pbkdf2_sha256$1000$salt$aGVsbG8=",  # digest of the wrong length
            # Far more iterations than any real hash: every sign-in would burn seconds.
            f"pbkdf2_sha256$50000000$salt${base64.b64encode(bytes(32)).decode()}",
            "pbkdf2_sha256$",
        ],
    )
    def test_refuses_anything_that_is_not_a_sane_pbkdf2_hash(self, value):
        with pytest.raises(CredentialImportError):
            DjangoPBKDF2SHA256Hasher().from_source({"password": value})


class TestForeignBcryptHasher:
    def test_php_style_hash_verifies_and_is_namespaced(self):
        php = "$2y$" + bcrypt.hashpw(b"hunter22", bcrypt.gensalt(4)).decode()[4:]
        hasher = ForeignBcryptHasher()
        encoded = hasher.from_source({"hash": php})
        assert encoded == f"bcrypt_foreign${php}"
        assert hasher.verify("hunter22", encoded) is True
        assert hasher.verify("hunter2", encoded) is False
        assert hasher.must_update(encoded) is True

    def test_refuses_a_non_bcrypt_value(self):
        with pytest.raises(CredentialImportError):
            ForeignBcryptHasher().from_source({"hash": "5f4dcc3b5aa765d61d8327deb882cf99"})


# ---------------------------------------------------------------------------
# The plugin base class
# ---------------------------------------------------------------------------


class TestWrappedLegacyHasher:
    def test_the_guide_example_round_trips(self):
        hasher = AcmeTrackerMd5()
        digest = hashlib.md5(b"NaCl" + b"open sesame").hexdigest()
        encoded = hasher.from_source({"salt": "NaCl", "password_digest": digest, "ignored": "x"})
        assert encoded.startswith("acme_md5$salt=NaCl$$2b$")
        assert hasher.verify("open sesame", encoded) is True
        assert hasher.verify("open sesame!", encoded) is False

    def test_parameters_with_separators_survive_the_round_trip(self):
        salt = "a$b,c=d%e"
        digest = hashlib.md5((salt + "pw").encode()).hexdigest()
        encoded = AcmeTrackerMd5().from_source({"salt": salt, "password_digest": digest})
        assert encoded.count("$") == 1 + 1 + 3  # algorithm, params, then bcrypt's own three
        assert AcmeTrackerMd5().verify("pw", encoded) is True

    def test_a_long_digest_is_not_silently_truncated_by_bcrypt(self):
        """Two digests sharing their first 72 bytes must not verify as each other."""

        class Sha512Hex(WrappedLegacyHasher):
            algorithm = "sha512_hex"

            def source_params(self, fields):
                return {}, fields["digest"]

            def inner_digest(self, password, params):
                return hashlib.sha512(password.encode()).hexdigest() + "x" * 10 + password

        hasher = Sha512Hex()
        stored = hasher.from_source({"digest": hasher.inner_digest("alpha", {})})
        assert hasher.verify("alpha", stored) is True
        assert hasher.verify("alphb", stored) is False

    def test_a_value_too_long_for_the_column_is_refused(self):
        with pytest.raises(CredentialImportError):
            AcmeTrackerMd5().from_source({"salt": "s" * MAX_ENCODED_LENGTH, "password_digest": "d"})

    def test_verify_never_raises_even_if_the_subclass_does(self):
        class Broken(WrappedLegacyHasher):
            algorithm = "broken"

            def source_params(self, fields):
                return {}, "digest"

            def inner_digest(self, password, params):
                raise RuntimeError("boom")

        encoded = Broken().from_source({})
        assert Broken().verify("anything", encoded) is False


# ---------------------------------------------------------------------------
# identify()
# ---------------------------------------------------------------------------


class TestIdentify:
    def test_native_bcrypt_prefixes_are_the_preferred_hasher(self):
        registry = get_password_hasher_registry()
        native = hash_password("pw")
        assert registry.identify(native) is registry.preferred
        assert registry.identify("$2a$" + native[4:]) is registry.preferred
        assert registry.identify("$2y$" + native[4:]) is registry.preferred

    def test_a_legacy_prefix_is_its_hasher(self):
        assert isinstance(get_password_hasher_registry().identify(_redmine_hash()), RedmineSHA1WrappedHasher)

    @pytest.mark.parametrize("value", [None, "", "abc", "unregistered_algo$x$y", "Bad$x", "$$", "$1$md5crypt"])
    def test_garbage_is_nothing(self, value):
        assert get_password_hasher_registry().identify(value) is None


# ---------------------------------------------------------------------------
# verify_password: never raises, never leaks timing
# ---------------------------------------------------------------------------

MALFORMED = [
    None,
    "",
    "abc",
    "$2b$xx$",
    "$2b$04$tooshort",
    "$2b$99$" + "a" * 53,
    "redmine_sha1$",
    "redmine_sha1$salt$not-bcrypt",
    "redmine_sha1$$$",
    "redmine_sha1" + "$" * 10,
    "django_pbkdf2_sha256$pbkdf2_sha256$1$s$!!!",
    "bcrypt_foreign$garbage",
    "unknown_algo$whatever",
    "\x00\x01\x02",
    "x" * 300,
]


class TestVerifyPassword:
    @pytest.mark.parametrize("encoded", MALFORMED)
    def test_malformed_values_are_no_match_not_an_error(self, monkeypatch, encoded):
        """Regression: bcrypt.checkpw raised ValueError('Invalid salt'), turning a login into a 500."""
        _enable(monkeypatch, "redmine_sha1", "django_pbkdf2_sha256", "bcrypt_foreign")
        assert verify_password("anything", encoded) is False

    def test_truncated_real_hash_is_no_match(self, monkeypatch):
        _enable(monkeypatch, "redmine_sha1")
        assert verify_password(PASSWORD, _redmine_hash()[:-5]) is False
        assert verify_password("pw", hash_password("pw")[:40]) is False

    def test_native_hash_verifies_without_any_allowlist(self):
        assert verify_password("pw", hash_password("pw")) is True

    def test_legacy_hash_verifies_once_enabled(self, monkeypatch):
        encoded = _redmine_hash()
        assert verify_password(PASSWORD, encoded) is False
        _enable(monkeypatch, "redmine_sha1")
        assert verify_password(PASSWORD, encoded) is True
        assert verify_password("wrong", encoded) is False

    def test_registered_but_not_enabled_never_verifies(self, monkeypatch):
        _enable(monkeypatch, "django_pbkdf2_sha256")
        assert verify_password(PASSWORD, _redmine_hash()) is False

    def test_after_the_cutoff_a_legacy_hash_fails(self, monkeypatch):
        _enable(monkeypatch, "redmine_sha1")
        settings = get_settings()
        monkeypatch.setattr(settings, "legacy_password_hashes_expire_at", datetime.now(UTC) - timedelta(minutes=1))
        assert verify_password(PASSWORD, _redmine_hash()) is False
        # Native hashes are untouched by the cutoff.
        assert verify_password("pw", hash_password("pw")) is True

    def test_before_the_cutoff_a_legacy_hash_still_works(self, monkeypatch):
        _enable(monkeypatch, "redmine_sha1")
        settings = get_settings()
        monkeypatch.setattr(settings, "legacy_password_hashes_expire_at", datetime.now(UTC) + timedelta(days=1))
        assert verify_password(PASSWORD, _redmine_hash()) is True

    def test_a_hasher_that_raises_is_no_match(self, monkeypatch):
        class Exploding:
            algorithm = "exploding"

            def verify(self, password, encoded):
                raise RuntimeError("plugin bug")

            def must_update(self, encoded):
                return True

            def from_source(self, fields):
                return "exploding$x"

        monkeypatch.setattr(hashers_mod, "_registry", build_password_hasher_registry([("buggy", [Exploding()])]))
        _enable(monkeypatch, "exploding")
        assert verify_password("pw", "exploding$x") is False

    @pytest.mark.parametrize(
        ("encoded", "enabled", "expired"),
        [
            pytest.param(None, (), False, id="no-hash"),
            pytest.param("unknown_algo$x", (), False, id="unregistered"),
            pytest.param("garbage", (), False, id="unrecognised"),
            pytest.param("$2b$04$short", (), False, id="malformed-bcrypt"),
            pytest.param("REDMINE", (), False, id="legacy-not-enabled"),
            pytest.param("REDMINE", ("redmine_sha1",), True, id="legacy-expired"),
            pytest.param("REDMINE", ("redmine_sha1",), False, id="legacy-wrong-password"),
            pytest.param("NATIVE", (), False, id="native-wrong-password"),
        ],
    )
    def test_every_failure_costs_exactly_one_bcrypt(self, monkeypatch, encoded, enabled, expired):
        """No failure path is faster than a real check, so timing reveals nothing."""
        value = {"REDMINE": _redmine_hash(), "NATIVE": hash_password("pw")}.get(encoded, encoded)
        _enable(monkeypatch, *enabled)
        if expired:
            monkeypatch.setattr(get_settings(), "legacy_password_hashes_expire_at", datetime.now(UTC))
        verify_password("warm-up", None)  # build the cached throwaway hash outside the count

        counter = _CountingCheckpw(monkeypatch)
        assert verify_password("not the password", value) is False
        assert counter.calls == 1

    def test_a_legacy_success_costs_one_bcrypt_like_a_native_one(self, monkeypatch):
        _enable(monkeypatch, "redmine_sha1")
        legacy, native = _redmine_hash(), hash_password("pw")
        counter = _CountingCheckpw(monkeypatch)
        assert verify_password(PASSWORD, legacy) is True
        assert counter.calls == 1
        assert verify_password("pw", native) is True
        assert counter.calls == 2


# ---------------------------------------------------------------------------
# password_needs_rehash and verify_and_update
# ---------------------------------------------------------------------------


class TestNeedsRehash:
    def test_current_bcrypt_does_not(self):
        assert password_needs_rehash(hash_password("pw")) is False

    def test_bcrypt_at_another_cost_does(self, monkeypatch):
        """Regression: nothing called this, so a changed BCRYPT_ROUNDS never reached anyone."""
        old = hash_password("pw")
        monkeypatch.setattr(get_settings(), "bcrypt_rounds", 5)
        assert password_needs_rehash(old) is True

    def test_a_foreign_bcrypt_prefix_does(self):
        assert password_needs_rehash("$2a$" + hash_password("pw")[4:]) is True

    def test_a_legacy_hash_does(self):
        assert password_needs_rehash(_redmine_hash()) is True

    @pytest.mark.parametrize("value", ["$2b$xx$", "$2b$", "garbage"])
    def test_garbage_does_not_raise(self, value):
        """Regression: ``int(parts[2])`` raised ValueError on a non-numeric cost."""
        assert password_needs_rehash(value) is True

    def test_no_hash_needs_nothing(self):
        assert password_needs_rehash(None) is False


class TestVerifyAndUpdate:
    def test_a_legacy_hash_is_replaced_by_native_bcrypt(self, monkeypatch):
        _enable(monkeypatch, "redmine_sha1")
        user = _user(_redmine_hash())
        check = verify_and_update(user, PASSWORD)
        assert check.matched and check.upgraded_from == "redmine_sha1"
        assert user.password_hash.startswith("$2b$04$")
        assert verify_password(PASSWORD, user.password_hash) is True

    def test_a_wrong_password_changes_nothing(self, monkeypatch):
        _enable(monkeypatch, "redmine_sha1")
        encoded = _redmine_hash()
        user = _user(encoded)
        check = verify_and_update(user, "wrong")
        assert not check and check.upgraded_from is None
        assert user.password_hash == encoded

    def test_an_outdated_bcrypt_cost_is_upgraded(self, monkeypatch):
        user = _user(hash_password("pw"))
        monkeypatch.setattr(get_settings(), "bcrypt_rounds", 5)
        check = verify_and_update(user, "pw")
        assert check.upgraded_from == "bcrypt"
        assert user.password_hash.startswith("$2b$05$")

    def test_a_current_hash_is_left_alone(self):
        encoded = hash_password("pw")
        user = _user(encoded)
        check = verify_and_update(user, "pw")
        assert check.matched and check.upgraded_from is None
        assert user.password_hash == encoded


# ---------------------------------------------------------------------------
# Registry rules and the bootstrap
# ---------------------------------------------------------------------------


class _Named:
    """A minimal hasher with a configurable id."""

    def __init__(self, algorithm):
        self.algorithm = algorithm

    def verify(self, password, encoded):
        return False

    def must_update(self, encoded):
        return True

    def from_source(self, fields):
        return f"{self.algorithm}$"


class TestRegistryRules:
    def test_builtins_are_registered_as_core(self):
        registry = build_password_hasher_registry()
        assert set(registry.legacy()) == {"redmine_sha1", "django_pbkdf2_sha256", "bcrypt_foreign"}
        assert {entry.origin for entry in registry.legacy().values()} == {"core"}
        assert isinstance(registry.preferred, BcryptHasher)

    def test_the_registry_cannot_be_changed_after_it_is_built(self):
        registry = build_password_hasher_registry()
        with pytest.raises(TypeError):
            registry.legacy()["acme_md5"] = object()  # type: ignore[index]
        assert not hasattr(registry, "register")

    @pytest.mark.parametrize("algorithm", ["redmine_sha1", "bcrypt_foreign", "bcrypt", "2b", "2y"])
    def test_reserved_and_builtin_ids_cannot_be_claimed(self, algorithm):
        with pytest.raises(PasswordHasherError):
            build_password_hasher_registry([("rogue", [_Named(algorithm)])])

    @pytest.mark.parametrize("algorithm", ["$2b$", "Acme", "a", "has-dash", "x" * 40, "1abc", "", None, "a$b"])
    def test_invalid_ids_are_refused(self, algorithm):
        with pytest.raises(PasswordHasherError):
            build_password_hasher_registry([("rogue", [_Named(algorithm)])])

    def test_a_duplicate_id_is_an_error_not_an_override(self):
        with pytest.raises(PasswordHasherError, match="registered twice"):
            build_password_hasher_registry([("one", [_Named("acme_md5")]), ("two", [_Named("acme_md5")])])

    def test_a_setting_path_and_a_plugin_cannot_share_an_id(self):
        with pytest.raises(PasswordHasherError, match="registered twice"):
            build_password_hasher_registry(
                [("acme-hashers", [AcmeTrackerMd5()])],
                ["tests.fixtures.acme_hashers.AcmeTrackerMd5"],
            )

    def test_something_that_is_not_a_hasher_is_refused(self):
        with pytest.raises(PasswordHasherError, match="not a password hasher"):
            build_password_hasher_registry([("rogue", [object()])])

    def test_an_unimportable_setting_path_is_refused(self):
        with pytest.raises(PasswordHasherError):
            build_password_hasher_registry(setting_paths=["tests.fixtures.acme_hashers.Missing"])


class TestBootstrap:
    def test_a_plugin_hasher_is_registered(self):
        settings = get_settings().model_copy(update={"installed_plugins": ["tests.fixtures.acme_hashers.Plugin"]})
        pm = bootstrap(settings)
        assert [plugin.name for plugin in pm.plugins] == ["acme-hashers"]
        entry = get_password_hasher_registry().legacy()["acme_md5"]
        assert entry.origin == "plugin:acme-hashers"

    def test_a_setting_path_hasher_is_registered(self):
        settings = get_settings().model_copy(
            update={"password_hashers": ["tests.fixtures.acme_hashers.AcmeTrackerMd5"]}
        )
        bootstrap(settings)
        entry = get_password_hasher_registry().legacy()["acme_md5"]
        assert entry.origin == "setting:tests.fixtures.acme_hashers.AcmeTrackerMd5"

    def test_the_celery_worker_bootstraps_plugins(self, monkeypatch):
        from specivo.tasks import _bootstrap_worker

        monkeypatch.setattr(get_settings(), "installed_plugins", ["tests.fixtures.acme_hashers.Plugin"])
        _bootstrap_worker()
        assert get_password_hasher_registry().get("acme_md5") is not None

    def test_a_bad_hasher_fails_the_bootstrap(self):
        settings = get_settings().model_copy(
            update={
                "installed_plugins": ["tests.fixtures.acme_hashers.Plugin"],
                "password_hashers": ["tests.fixtures.acme_hashers.AcmeTrackerMd5"],
            }
        )
        with pytest.raises(PasswordHasherError):
            bootstrap(settings)

    def test_a_plugin_hasher_verifies_end_to_end_once_enabled(self, monkeypatch):
        bootstrap(get_settings().model_copy(update={"installed_plugins": ["tests.fixtures.acme_hashers.Plugin"]}))
        digest = hashlib.md5(b"NaCl" + b"open sesame").hexdigest()
        encoded = (
            get_password_hasher_registry().get("acme_md5").from_source({"salt": "NaCl", "password_digest": digest})
        )
        user = _user(encoded)
        assert verify_and_update(user, "open sesame").matched is False  # registered, not enabled
        _enable(monkeypatch, "acme_md5")
        check = verify_and_update(user, "open sesame")
        assert check.upgraded_from == "acme_md5"
        assert user.password_hash.startswith("$2b$")


# ---------------------------------------------------------------------------
# specivo.cli.admin password-hashers test
# ---------------------------------------------------------------------------


class TestHasherTestCommand:
    def _run(self, algorithm, fields, password):
        from specivo.cli.admin import _run_password_hasher_test

        args = argparse.Namespace(algorithm=algorithm, field=fields, password=password)
        return _run_password_hasher_test(args)

    def test_a_known_vector_matches(self, capsys):
        code = self._run("redmine_sha1", [f"salt={SALT}", f"hashed_password={REDMINE_DIGEST}"], PASSWORD)
        assert code == 0
        out = capsys.readouterr().out
        assert "MATCH" in out and "NO MATCH" not in out
        assert REDMINE_DIGEST not in out

    def test_a_wrong_password_does_not(self, capsys):
        code = self._run("redmine_sha1", [f"salt={SALT}", f"hashed_password={REDMINE_DIGEST}"], "nope")
        assert code == 1
        assert "NO MATCH" in capsys.readouterr().out

    def test_an_unknown_algorithm_is_an_error(self):
        assert self._run("nope_algo", [], "x") == 2

    def test_bad_fields_are_an_error(self):
        assert self._run("redmine_sha1", ["salt=only"], "x") == 2


class TestGuideExample:
    """The guide's plugin example is the fixture module, and its CLI example matches."""

    GUIDE = "docs/guide/install/password-hasher-plugins.md"

    def _guide(self) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[3] / self.GUIDE).read_text()

    def test_the_guide_shows_the_tested_module_verbatim(self):
        import ast
        import inspect
        import re

        import tests.fixtures.acme_hashers as example

        source = inspect.getsource(example)
        docstring = ast.get_docstring(ast.parse(source), clean=False)
        body = source.split(f'"""{docstring}"""', 1)[1].strip()
        blocks = re.findall(r"```python\n(.*?)```", self._guide(), flags=re.S)
        assert body in [block.strip() for block in blocks]

    def test_the_guide_cli_example_matches(self, monkeypatch):
        from specivo.cli.admin import _run_password_hasher_test

        monkeypatch.setattr(get_settings(), "password_hashers", ["tests.fixtures.acme_hashers.AcmeTrackerMd5"])
        args = argparse.Namespace(
            algorithm="acme_md5",
            field=["salt=NaCl", "password_digest=ce48c13da732f51a1bf9c4fac876c638"],
            password="open sesame",
        )
        assert _run_password_hasher_test(args) == 0
        assert "--field password_digest=ce48c13da732f51a1bf9c4fac876c638" in self._guide()
