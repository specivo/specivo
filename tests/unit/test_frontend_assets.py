"""Unit tests for loading the generated esbuild bundle manifests."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from specivo.web.assets import (
    load_asset_manifests,
    missing_asset_manifests,
    warn_if_bundles_missing,
)

pytestmark = pytest.mark.unit


def _write_manifest(dist: Path, sub: str, data: dict[str, str]) -> None:
    (dist / sub).mkdir(parents=True, exist_ok=True)
    (dist / sub / "manifest.json").write_text(json.dumps(data))


def test_unbuilt_dist_reports_every_manifest_missing(tmp_path: Path):
    dist = tmp_path / "dist"

    assert missing_asset_manifests(dist) == ["js", "css"]
    assert load_asset_manifests(dist) == {}


def test_manifests_are_merged(tmp_path: Path):
    _write_manifest(tmp_path, "js", {"app.min.js": "app.min.1a2b3c4d.js"})
    _write_manifest(tmp_path, "css", {"specivo.min.css": "specivo.min.5e6f7a8b.css"})

    assert missing_asset_manifests(tmp_path) == []
    assert load_asset_manifests(tmp_path) == {
        "app.min.js": "app.min.1a2b3c4d.js",
        "specivo.min.css": "specivo.min.5e6f7a8b.css",
    }


def test_partial_build_loads_what_exists(tmp_path: Path):
    _write_manifest(tmp_path, "js", {"app.min.js": "app.min.js"})

    assert missing_asset_manifests(tmp_path) == ["css"]
    assert load_asset_manifests(tmp_path) == {"app.min.js": "app.min.js"}


def test_warns_when_bundles_are_missing(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger="specivo.web.assets"):
        assert warn_if_bundles_missing(tmp_path) is True

    assert "Frontend bundles not built" in caplog.text
    assert "make frontend-build" in caplog.text


def test_silent_when_bundles_are_built(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    _write_manifest(tmp_path, "js", {"app.min.js": "app.min.js"})
    _write_manifest(tmp_path, "css", {"specivo.min.css": "specivo.min.css"})

    with caplog.at_level(logging.WARNING, logger="specivo.web.assets"):
        assert warn_if_bundles_missing(tmp_path) is False

    assert caplog.records == []
