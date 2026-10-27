"""The ``sso_idp`` marker: declared, and auto-skipped when the disposable IdP is down.

Tests against the real IdP (``make sso-idp-up``) must never fail a normal
``make test`` run. The collection hook in ``tests/conftest.py`` probes the
issuer's discovery document once and skips every ``sso_idp`` test when it is
not reachable.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

import tests.conftest as root_conftest

pytestmark = pytest.mark.unit


class _FakeItem:
    """The slice of ``pytest.Item`` the collection hook uses."""

    def __init__(self, *markers: str) -> None:
        self._markers = {name: getattr(pytest.mark, name).mark for name in markers}
        self.added: list[Any] = []

    def get_closest_marker(self, name: str) -> pytest.Mark | None:
        return self._markers.get(name)

    def add_marker(self, marker: Any) -> None:
        self.added.append(marker)


def _collect(*items: _FakeItem) -> None:
    """Run the root conftest's collection hook over fake items."""
    root_conftest.pytest_collection_modifyitems(cast(pytest.Config, None), cast(list[pytest.Item], list(items)))


def _skip_reasons(item: _FakeItem) -> list[str]:
    return [m.mark.kwargs.get("reason", "") for m in item.added if m.mark.name == "skip"]


def test_marker_is_declared(pytestconfig: pytest.Config) -> None:
    declared = [line.split(":", 1)[0].strip() for line in pytestconfig.getini("markers")]
    assert "sso_idp" in declared


def test_sso_idp_test_is_skipped_when_issuer_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(root_conftest, "_issuer_is_up", lambda issuer, timeout=1.0: False)
    idp_item, plain_item = _FakeItem("sso_idp"), _FakeItem("unit")

    _collect(idp_item, plain_item)

    reasons = _skip_reasons(idp_item)
    assert len(reasons) == 1
    assert "start it with: make sso-idp-up" in reasons[0]
    assert root_conftest._SSO_TEST_ISSUER in reasons[0]
    assert plain_item.added == []


def test_sso_idp_test_runs_when_issuer_is_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(root_conftest, "_issuer_is_up", lambda issuer, timeout=1.0: True)
    idp_item = _FakeItem("sso_idp")

    _collect(idp_item)

    assert idp_item.added == []


def test_issuer_is_not_probed_without_sso_idp_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(issuer: str, timeout: float = 1.0) -> bool:
        raise AssertionError("the IdP must not be probed when no sso_idp test was collected")

    monkeypatch.setattr(root_conftest, "_issuer_is_up", _fail)

    _collect(_FakeItem("unit"))


def test_probe_reports_unreachable_issuer_as_down() -> None:
    # Port 9 (discard) is not served on a test host; the probe must answer False, not raise.
    assert root_conftest._issuer_is_up("http://127.0.0.1:9/realms/none", timeout=0.2) is False
