# ADR-0005: Responsive Design Testing Strategy

**Date:** 2026-04-11
**Status:** Accepted
**Deciders:** Boris

## Context

Specivo's E2E tests (ADR-0003) run at the browser's default viewport size — they verify functional behavior but don't test layout at different screen sizes. The CSS defines 4 breakpoints (1100px, 960px, 768px, 480px) with significant layout changes at each: sidebar collapses to off-canvas, grids go single-column, table columns hide, page headers stack vertically. These layout transitions were only tested manually (phone or Chrome DevTools) and any regression was caught reactively.

Needed: a systematic way to document responsive behavior per page type and catch layout regressions automatically.

## Decision

### Breakpoint matrix documentation

A wiki page ("Responsive Design Spec" in the SPECIVO project) documents what each UI element does at each breakpoint. Format: one table per page type, rows = elements, columns = breakpoint tiers. This is the source of truth for what "correct" looks like at each size.

### Four test viewports

Tests run at 4 viewport sizes covering all CSS breakpoints:

| Name | Size | Covers |
|------|------|--------|
| mobile | 375×812 | ≤480px — maximum column hiding, stacked headers |
| tablet | 768×1024 | ≤768px — sidebar off-canvas, some columns hidden |
| narrow | 960×800 | ≤960px — issue detail / analytics collapse |
| desktop | 1280×800 | >1100px — full multi-column layout |

Defined in `tests/e2e/conftest.py` as `VIEWPORTS` dict. The `responsive_page` fixture creates an admin-authed browser context at the parametrized viewport size.

### Seed data isolation

Responsive and visual tests use a dedicated project (`RTEST`) with deterministic seed data: 8 issues, a wiki home page. The `responsive_project` session-scoped fixture creates this idempotently. This avoids dependency on dev server data or other test side effects.

### Two test layers

**1. Structural assertions** (`test_mobile_responsive.py`)

CSS property checks that run at all 4 viewports via `@pytest.mark.parametrize`:
- No horizontal overflow on any page
- Buttons within viewport bounds
- Grid columns match expected layout (single vs multi-column)
- Elements visible/hidden as specified in the breakpoint matrix

Plus viewport-specific classes for mobile-only checks (sidebar hidden, hamburger visible) and desktop-only checks (sidebar visible, two-column issue detail).

**2. Visual regression baselines** (`test_visual_regression.py`)

Screenshot comparison for 7 page types × 4 viewports = 28 baselines:
- `page.screenshot()` captures viewport-only (not full page — avoids size variance from dynamic content)
- Baseline PNGs stored in `tests/e2e/snapshots/` (committed to repo)
- Comparison uses Pillow pixel-level diff with per-channel tolerance of 5 and max 2% differing pixels
- `UPDATE_SNAPSHOTS=1` environment variable regenerates baselines

### Why not `expect(page).to_have_screenshot()`

Playwright Python's `PageAssertions` doesn't include `to_have_screenshot()` in our version (1.58.0 / pytest-playwright 0.7.2). Manual comparison with Pillow gives us control over tolerance without adding dependencies.

### Why viewport-only screenshots (not full page)

Full-page screenshots change height when other tests create data that appears on shared pages (dashboard). Viewport-only captures are stable regardless of content volume below the fold.

## Consequences

**Positive:**
- Layout regressions caught automatically at 4 breakpoints (was: only manual testing)
- Breakpoint matrix wiki page serves as design spec and test oracle
- Visual regression detects subtle CSS changes that structural assertions miss
- Same pytest/Playwright ecosystem — no new tools
- `make test-e2e-update-snapshots` for easy baseline updates after intentional changes

**Negative:**
- 77 structural + 28 visual = 105 additional test runs (~50s total)
- Snapshot baselines must be regenerated after any visual change
- 2% pixel tolerance may miss very subtle regressions; byte-exact comparison would catch more but produces false positives from anti-aliasing
- Screenshots are OS/font-dependent — baselines generated on macOS won't match Linux CI (mitigate by regenerating in CI environment)

## Makefile targets

```makefile
make test-e2e                    # Runs all E2E including responsive + visual
make test-e2e-update-snapshots   # Regenerate visual baselines
```

## File locations

| Component | Path |
|-----------|------|
| Viewport definitions + fixtures | `tests/e2e/conftest.py` |
| Structural responsive tests | `tests/e2e/test_mobile_responsive.py` |
| Visual regression tests | `tests/e2e/test_visual_regression.py` |
| Screenshot baselines | `tests/e2e/snapshots/*.png` |
| Breakpoint matrix (wiki) | SPECIVO project → "Responsive Design Spec" |
