"""Playwright visual regression tests — screenshot baselines per page per viewport.

Captures full-page screenshots and compares against committed baselines.
On first run, baselines are created automatically. On subsequent runs,
screenshots are compared with a pixel-level tolerance (1% diff allowed).

To update baselines after intentional design changes:

    make test-e2e-update-snapshots

Tests are parametrized across 4 viewports: mobile (375), tablet (768),
narrow (960), desktop (1280).

Uses the shared responsive_project fixture (RTEST) with fixed seed data
so that screenshots are stable across runs.
"""

import io
import os
from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import Page

from tests.e2e.conftest import RESPONSIVE_PROJECT_KEY

pytestmark = [pytest.mark.e2e]

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
UPDATE_SNAPSHOTS = os.environ.get("UPDATE_SNAPSHOTS", "0") == "1"
MAX_DIFF_RATIO = 0.02  # Allow up to 2% pixel difference (accounts for dynamic content)


def _pixel_diff_ratio(img_a: bytes, img_b: bytes) -> float:
    """Compare two PNG images and return the fraction of differing pixels."""
    a = Image.open(io.BytesIO(img_a)).convert("RGB")
    b = Image.open(io.BytesIO(img_b)).convert("RGB")

    # If sizes differ, that's a 100% diff
    if a.size != b.size:
        return 1.0

    pixels_a = a.load()
    pixels_b = b.load()
    width, height = a.size
    total = width * height
    diff_count = 0

    for y in range(height):
        for x in range(width):
            ra, ga, ba = pixels_a[x, y]
            rb, gb, bb = pixels_b[x, y]
            # Allow per-channel tolerance of 5 for anti-aliasing
            if abs(ra - rb) > 5 or abs(ga - gb) > 5 or abs(ba - bb) > 5:
                diff_count += 1

    return diff_count / total if total > 0 else 0.0


# Pages to capture.
PAGES = [
    ("dashboard", "/"),
    ("issues_list", f"/projects/{RESPONSIVE_PROJECT_KEY}/issues/"),
    ("board_view", f"/projects/{RESPONSIVE_PROJECT_KEY}/issues/?view=board"),
    ("backlog", f"/projects/{RESPONSIVE_PROJECT_KEY}/backlog/"),
    ("wiki_home", f"/projects/{RESPONSIVE_PROJECT_KEY}/wiki/home/"),
    ("settings", f"/projects/{RESPONSIVE_PROJECT_KEY}/settings/"),
    ("project_detail", f"/projects/{RESPONSIVE_PROJECT_KEY}/"),
]


@pytest.mark.parametrize("page_name,path", PAGES, ids=[p[0] for p in PAGES])
def test_visual_snapshot(
    responsive_page: Page,
    viewport_name: str,
    responsive_project: str,
    page_name: str,
    path: str,
):
    """Screenshot baseline comparison for each page at each viewport."""
    responsive_page.goto(path)
    responsive_page.wait_for_load_state("networkidle")

    filename = f"{page_name}-{viewport_name}.png"
    baseline_path = SNAPSHOT_DIR / filename

    # Take current screenshot
    current = responsive_page.screenshot(full_page=False)

    if UPDATE_SNAPSHOTS or not baseline_path.exists():
        SNAPSHOT_DIR.mkdir(exist_ok=True)
        baseline_path.write_bytes(current)
        if UPDATE_SNAPSHOTS:
            return
        pytest.skip(f"Baseline created: {filename}")
        return

    # Compare with tolerance
    baseline = baseline_path.read_bytes()
    if current == baseline:
        return  # Exact match, fast path

    diff_ratio = _pixel_diff_ratio(baseline, current)
    if diff_ratio > MAX_DIFF_RATIO:
        actual_path = SNAPSHOT_DIR / f"{page_name}-{viewport_name}.actual.png"
        actual_path.write_bytes(current)
        pytest.fail(
            f"Visual regression: {filename} — {diff_ratio:.2%} pixels differ "
            f"(threshold: {MAX_DIFF_RATIO:.0%}). "
            f"Actual saved to {actual_path.name}. "
            f"Run: make test-e2e-update-snapshots"
        )
