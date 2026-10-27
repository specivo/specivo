"""The SSO guide documents every error code a person or an administrator can see."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from specivo.services.sso import errors
from specivo.services.sso.identity_service import LastSignInMethodError

pytestmark = pytest.mark.unit

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "guide" / "install" / "single-sign-on" / "index.md"

# The base class's code is never raised on its own.
_NOT_RAISED = {"sso_error"}


def _all_subclasses(cls: type) -> set[type]:
    found: set[type] = set()
    for sub in cls.__subclasses__():
        found.add(sub)
        found |= _all_subclasses(sub)
    return found


def _defined_codes() -> set[str]:
    codes = {sub.default_code for sub in _all_subclasses(errors.SsoError) if hasattr(sub, "default_code")}
    codes.add(LastSignInMethodError().code)
    return codes - _NOT_RAISED


def _documented_codes() -> set[str]:
    text = GUIDE.read_text(encoding="utf-8")
    return set(re.findall(r"^\| `(sso_[a-z_]+)` \|", text, flags=re.MULTILINE))


def test_every_sso_error_code_is_in_the_troubleshooting_table() -> None:
    missing = _defined_codes() - _documented_codes()
    assert not missing, f"document these codes in {GUIDE.name}: {sorted(missing)}"


def test_the_troubleshooting_table_lists_no_unknown_code() -> None:
    unknown = _documented_codes() - _defined_codes()
    assert not unknown, f"codes in the guide that the code does not define: {sorted(unknown)}"
