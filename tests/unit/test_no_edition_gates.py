"""Guards: Specivo has one edition, with no feature registry and no tier wording.

Everything ships in Specivo itself. These tests keep an edition gate, an
upgrade prompt or a ``tier`` field from coming back unnoticed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = _ROOT / "specivo"

#: Phrases that would tell a user a feature belongs to another edition.
_EDITION_PHRASES = ("Pro or Enterprise", "Specivo Pro", "Specivo Enterprise")


def test_features_module_is_gone():
    assert importlib.util.find_spec("specivo.core.features") is None


def test_templates_have_no_feature_gates():
    offenders = [
        str(path.relative_to(_ROOT))
        for path in (_PACKAGE / "templates").rglob("*.html")
        if "has_feature(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_no_tier_wording_in_templates_and_catalogues():
    sources = [*(_PACKAGE / "templates").rglob("*.html"), *(_PACKAGE / "locale").glob("*.pot")]
    assert sources, "templates and the catalogue template should be found"
    offenders = [
        f"{path.relative_to(_ROOT)}: {phrase}"
        for path in sources
        for phrase in _EDITION_PHRASES
        if phrase in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_health_response_has_no_tier():
    from specivo.schemas.common import HealthResponse

    assert "tier" not in HealthResponse.model_fields
    body = HealthResponse(status="ok", database="ok", redis="ok", version="").model_dump()
    assert set(body) == {"status", "database", "redis", "version"}
