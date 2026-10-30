"""The "copy branch name" builder (``specivo.services.branch_name``)."""

from __future__ import annotations

import re

import pytest

from specivo.hooks.refs import extract_branch_refs
from specivo.services.branch_name import (
    DEFAULT_BRANCH_FORMAT,
    BranchNameSettings,
    make_branch_name,
    slugify,
    validate_branch_format,
)

pytestmark = pytest.mark.unit


def is_valid_ref_name(name: str) -> bool:
    """``git check-ref-format --branch`` rules, for a name with no ``refs/heads/`` prefix."""
    if not name or name == "@" or name.startswith(("-", "/")) or name.endswith(("/", ".")):
        return False
    if ".." in name or "//" in name or "@{" in name:
        return False
    if re.search(r"[\x00-\x20\x7f~^:?*\[\\]", name):
        return False
    return all(part and not part.startswith(".") and not part.endswith(".lock") for part in name.split("/"))


def _name(subject: str, tracker: str = "Bug", key: str = "ACME-12", settings: BranchNameSettings | None = None) -> str:
    return make_branch_name(settings or BranchNameSettings(), tracker_name=tracker, display_key=key, subject=subject)


def test_default_format() -> None:
    assert DEFAULT_BRANCH_FORMAT == "{type}/{key}-{slug}"
    assert _name("Login fails on Safari") == "fix/ACME-12-login-fails-on-safari"


@pytest.mark.parametrize(
    ("tracker", "prefix"),
    [("Bug", "fix"), ("Feature", "feat"), ("Support", "chore"), ("bug", "fix"), ("Task", "chore")],
)
def test_type_from_tracker(tracker: str, prefix: str) -> None:
    assert _name("x", tracker=tracker).startswith(prefix + "/")


def test_type_mapping_is_configurable() -> None:
    settings = BranchNameSettings(types={"Task": "task", "Bug": "bugfix"}, default_type="misc")
    assert _name("x", tracker="Task", settings=settings) == "task/ACME-12-x"
    assert _name("x", tracker="Bug", settings=settings) == "bugfix/ACME-12-x"
    assert _name("x", tracker="Feature", settings=settings) == "misc/ACME-12-x"


def test_long_subject_is_truncated() -> None:
    name = _name("A very long subject that goes on and on well past the forty character limit")
    slug = name.removeprefix("fix/ACME-12-")
    assert len(slug) <= 40
    assert not slug.endswith("-")
    assert slug == "a-very-long-subject-that-goes-on-and-on"


def test_thai_subject_falls_back_to_type_and_key() -> None:
    assert _name("แก้ไขหน้าเข้าสู่ระบบ") == "fix/ACME-12"


@pytest.mark.parametrize(
    ("subject", "slug"),
    [
        ("Fix: the *parser*!!", "fix-the-parser"),
        ("  leading and trailing  ", "leading-and-trailing"),
        ("Café déjà vu", "cafe-deja-vu"),
        ("a..b//c~d^e:f?g*h[i]j\\k@{l", "a-b-c-d-e-f-g-h-i-j-k-l"),
        ("UPPER lower 123", "upper-lower-123"),
        ("---", ""),
    ],
)
def test_slug(subject: str, slug: str) -> None:
    assert slugify(subject) == slug


@pytest.mark.parametrize(
    "fmt",
    ["{type}/{key}-{slug}", "{key}", "{key}/{slug}", "feature/{key}_{slug}", "{type}-{key}"],
)
def test_valid_formats(fmt: str) -> None:
    assert validate_branch_format(fmt) == fmt


@pytest.mark.parametrize(
    "fmt",
    [
        "{type}/{slug}",
        "",
        "{key}/{unknown}",
        "{key} {slug}",
        "{key}..{slug}",
        "/{key}",
        "{key}~1",
        "{key}" + "x" * 200,
        "{key",
    ],
)
def test_invalid_formats(fmt: str) -> None:
    with pytest.raises(ValueError):
        validate_branch_format(fmt)


@pytest.mark.parametrize(
    "subject",
    ["Login fails", "แก้ไข", ".hidden", "ends.lock", "a..b", "x" * 300, "@{weird}", "-dash", "dots...", "trailing."],
)
@pytest.mark.parametrize("fmt", ["{type}/{key}-{slug}", "{key}/{slug}", "{slug}.{key}", "{slug}-{key}"])
def test_output_is_a_valid_ref_name(subject: str, fmt: str) -> None:
    name = _name(subject, settings=BranchNameSettings(format=fmt))
    assert is_valid_ref_name(name), name


@pytest.mark.parametrize("fmt", ["{type}/{key}-{slug}", "{key}/{slug}", "{key}", "feature/{key}_{slug}"])
@pytest.mark.parametrize("subject", ["Login fails on Safari", "แก้ไข", "Fix bug 42-x"])
def test_round_trip_with_branch_parser(fmt: str, subject: str) -> None:
    name = _name(subject, key="ACME-12", settings=BranchNameSettings(format=fmt))
    refs = extract_branch_refs(name, frozenset({"ACME"}))
    assert [r.display_key for r in refs] == ["ACME-12"]


def test_settings_from_project_settings() -> None:
    settings = BranchNameSettings.from_project_settings(
        {"branch_name": {"format": "{key}/{slug}", "types": {"Bug": "hotfix"}, "default_type": "task"}}
    )
    assert settings.format == "{key}/{slug}"
    assert settings.type_for("bug") == "hotfix"
    assert settings.type_for("Other") == "task"


def test_settings_from_broken_project_settings_fall_back_to_defaults() -> None:
    settings = BranchNameSettings.from_project_settings({"branch_name": {"format": "{slug}", "types": "nope"}})
    assert settings == BranchNameSettings()


@pytest.mark.parametrize(
    "fmt",
    ["{type}{key}", "x{key}", "{key}{slug}", "{key}1", "{slug}{key}", "{key}x", "fix/v2{key}-{slug}"],
)
def test_key_glued_to_a_letter_digit_or_placeholder_is_rejected(fmt: str) -> None:
    # "{key}{slug}" with the subject "2fa" gives ACME-122fa: the parser would read ACME-122.
    with pytest.raises(ValueError):
        validate_branch_format(fmt)


@pytest.mark.parametrize("subject", ["2fa login", "Login", "แก้ไข", "9"])
@pytest.mark.parametrize("fmt", ["{slug}.{key}", "{slug}-{key}", "{type}_{key}", "{key}.{slug}", "{type}/{key}"])
def test_every_accepted_format_round_trips(fmt: str, subject: str) -> None:
    name = _name(subject, key="ACME-12", settings=BranchNameSettings(format=validate_branch_format(fmt)))
    assert [r.display_key for r in extract_branch_refs(name, frozenset({"ACME"}))] == ["ACME-12"]
