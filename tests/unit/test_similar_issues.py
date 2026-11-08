"""Pure helpers behind similar-issue suggestions (``specivo.services.similar_issues``)."""

from __future__ import annotations

import pytest

from specivo.services.similar_issues import normalize_query, project_setting_enabled, uses_recency_scan

pytestmark = pytest.mark.unit


def test_normalize_query_removes_nul_and_trims() -> None:
    assert normalize_query(" a\x00b ") == "a b"


def test_normalize_query_blank_is_empty() -> None:
    assert normalize_query(" \x00 ") == ""


@pytest.mark.parametrize(
    "settings",
    [None, {}, {"similar_issues": "junk"}, {"similar_issues": True}, {"similar_issues": None}],
)
def test_project_setting_defaults_on(settings) -> None:
    assert project_setting_enabled(settings) is True


def test_project_setting_false_turns_off() -> None:
    assert project_setting_enabled({"similar_issues": False}) is False


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("หน้าเข้าสู่ระบบโหลดไม่ได้", True),
        ("登录页面无法加载", True),
        ("ログインページが開かない", True),
        ("ການເຂົ້າສູ່ລະບົບ", True),
        ("ဝင်ရောက်ခြင်း", True),
        ("ការចូល", True),
        ("Login page does not load", False),
        ("로그인 페이지가 열리지 않음", False),
        ("Login 页面无法加载", True),
    ],
)
def test_uses_recency_scan_for_unsegmented_scripts(q: str, expected: bool) -> None:
    assert uses_recency_scan(q) is expected
