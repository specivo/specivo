"""Frontend bundle manifests (esbuild output under ``specivo/static/dist/``).

The bundles are generated, never committed: the Docker image, CI and the dev
watcher container build them. esbuild writes a ``manifest.json`` per output dir
mapping each logical bundle name to the served filename, e.g.
``{"app.min.js": "app.min.1a2b3c4d.js"}``. Templates resolve the served name via
the ``versioned`` Jinja global, which is filled from these manifests at startup.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DIST_DIR = Path(__file__).resolve().parent.parent / "static" / "dist"
MANIFEST_SUBDIRS = ("js", "css")


def load_asset_manifests(dist_dir: Path = DIST_DIR) -> dict[str, str]:
    """Merge the per-directory manifests into one logical -> served name map.

    Missing manifests are skipped, so the un-hashed defaults in
    ``specivo.web.deps._versioned_assets`` remain in effect for those bundles.
    """
    merged: dict[str, str] = {}
    for sub in MANIFEST_SUBDIRS:
        manifest = dist_dir / sub / "manifest.json"
        if manifest.exists():
            merged.update(json.loads(manifest.read_text()))
    return merged


def missing_asset_manifests(dist_dir: Path = DIST_DIR) -> list[str]:
    """Return the output subdirectories (``js``, ``css``) that have no manifest."""
    return [sub for sub in MANIFEST_SUBDIRS if not (dist_dir / sub / "manifest.json").exists()]


def warn_if_bundles_missing(dist_dir: Path = DIST_DIR) -> bool:
    """Log a warning when the frontend bundles have not been built.

    The app still starts — templates fall back to un-hashed bundle names — but
    pages load without Specivo's own CSS and JS. Returns True if anything is missing.
    """
    missing = missing_asset_manifests(dist_dir)
    if not missing:
        return False
    logger.warning(
        "Frontend bundles not built: no manifest.json in %s. Pages will load without "
        "Specivo's CSS and JS. Run `make frontend-build`, or `make dev-up`, which "
        "starts the bundle watcher.",
        ", ".join(str(dist_dir / sub) for sub in missing),
    )
    return True
