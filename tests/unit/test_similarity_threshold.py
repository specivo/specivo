"""Minimum similarity for semantic search hits: per-model defaults and the setting."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from specivo.core.config import Settings, get_settings
from specivo.services.similarity_threshold import default_min_similarity, min_similarity_for

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("model_name", "expected"),
    [
        ("multilingual-e5-small", 0.82),
        ("intfloat/multilingual-e5-large", 0.82),
        ("e5-base-v2", 0.82),
        ("mock-384", None),
        ("text-embedding-3-small", None),
        ("", None),
    ],
)
def test_default_min_similarity_per_model(model_name: str, expected: float | None) -> None:
    assert default_min_similarity(model_name) == expected


def test_model_default_applies_when_setting_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "search_semantic_min_similarity", None)
    assert min_similarity_for("multilingual-e5-small") == 0.82
    assert min_similarity_for("mock-384") is None


@pytest.mark.parametrize("configured", [0.5, -1.0, 0.0, 0.9])
def test_setting_overrides_every_model(monkeypatch: pytest.MonkeyPatch, configured: float) -> None:
    monkeypatch.setattr(get_settings(), "search_semantic_min_similarity", configured)
    assert min_similarity_for("multilingual-e5-small") == configured
    assert min_similarity_for("mock-384") == configured


def test_setting_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_SEMANTIC_MIN_SIMILARITY", "0.79")
    assert Settings().search_semantic_min_similarity == 0.79  # type: ignore[call-arg]


@pytest.mark.parametrize("value", ["1.5", "-1.1"])
def test_setting_outside_cosine_range_is_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SEARCH_SEMANTIC_MIN_SIMILARITY", value)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]
