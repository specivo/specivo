"""Unit tests for computed (project-derived) metadata helpers."""

from __future__ import annotations

from specivo.services.computed_metadata_service import (
    COMPUTED_METADATA_SETTINGS_KEY,
    computed_values,
    merge_computed,
    strip_computed,
)


def _settings(area: str | None = "Finance") -> dict:
    if area is None:
        return {}
    return {COMPUTED_METADATA_SETTINGS_KEY: {"Area": area}}


def test_computed_values_empty_for_none_or_missing():
    assert computed_values(None) == {}
    assert computed_values({}) == {}
    assert computed_values({"other": 1}) == {}


def test_computed_values_ignores_non_dict():
    assert computed_values({COMPUTED_METADATA_SETTINGS_KEY: "not-a-dict"}) == {}


def test_computed_values_returns_map():
    assert computed_values(_settings("Finance")) == {"Area": "Finance"}


def test_merge_overlays_computed_and_computed_wins():
    stored = {"priority_note": "x", "Area": "stale"}
    merged = merge_computed(stored, _settings("Finance"))
    assert merged == {"priority_note": "x", "Area": "Finance"}


def test_merge_with_no_computed_returns_stored_copy():
    stored = {"a": 1}
    merged = merge_computed(stored, {})
    assert merged == {"a": 1}
    assert merged is not stored  # copy, not alias


def test_strip_removes_computed_keys_only():
    metadata = {"Area": "anything", "keep": "me"}
    assert strip_computed(metadata, _settings("Finance")) == {"keep": "me"}


def test_strip_noop_when_no_computed_config():
    metadata = {"Area": "anything", "keep": "me"}
    assert strip_computed(metadata, {}) == {"Area": "anything", "keep": "me"}


def test_strip_handles_none_metadata():
    assert strip_computed(None, _settings("Finance")) == {}
